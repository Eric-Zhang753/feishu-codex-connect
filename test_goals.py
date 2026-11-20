import asyncio
from contextlib import suppress
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from test_bridge import b, config


class GoalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge=b.Bridge(dict(config(),shared_socket='/tmp/shared.sock'),Path(self.tmp.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply=AsyncMock(return_value=True)
        self.bridge.codex=AsyncMock()
        self.bridge.ensure_codex=AsyncMock()
        self.bridge.state.bind('owner')
        self.bridge.state.select_thread('chat','selected-thread','/tmp','preserved-model','openai','high')
        self.bridge.loaded_threads.add('selected-thread')
        self.goal=None
        self.api_calls=[]
        self.bridge.codex.request.side_effect=self.api

    async def api(self,method,params,**kwargs):
        self.api_calls.append((method,dict(params)))
        if method=='thread/goal/get':return {'goal':dict(self.goal) if self.goal else None}
        if method=='thread/goal/set':
            if self.goal is None:self.goal={'threadId':params['threadId'],'tokensUsed':0,'timeUsedSeconds':0,'tokenBudget':None}
            if 'objective' in params:self.goal['objective']=params['objective'];self.goal['tokensUsed']=0
            if 'status' in params:self.goal['status']=params['status']
            return {'goal':dict(self.goal)}
        if method=='thread/goal/clear':self.goal=None;return {}
        if method=='thread/start':return {'thread':{'id':'created-thread','cwd':'/tmp','model':'preserved-model'}}
        raise AssertionError(method)

    async def submit(self,text,user='owner'):
        msg={'message_id':str(time.time_ns()),'chat_id':'chat','user_id':user,'chat_type':'p2p',
             'text':text,'created':time.time()}
        self.bridge.state.receive(msg)
        await self.bridge.handle(msg)
        return msg

    def existing(self,status='active'):
        self.goal={'threadId':'selected-thread','objective':'原目标','status':status,'tokenBudget':None,
                   'tokensUsed':8400,'timeUsedSeconds':120}

    async def test_start_uses_native_goal_and_does_not_queue_or_start_duplicate_turn(self):
        await self.submit('/goal 完成实验并检查结果')
        self.assertEqual(self.api_calls[-1],('thread/goal/set',{
            'threadId':'selected-thread','origin':'user','objective':'完成实验并检查结果','status':'active'}))
        self.assertEqual(self.bridge.jobs.qsize(),0)
        self.assertFalse(any(method=='turn/start' for method,params in self.api_calls))
        self.assertEqual(self.bridge.state.context('chat')['model'],'preserved-model')
        self.assertTrue(self.bridge.goals.is_managed('selected-thread'))

    async def test_edit_during_active_work_is_immediate_and_preserves_running_state(self):
        self.existing()
        self.bridge.active={'chat_id':'chat','thread_id':'selected-thread','turn_id':'busy-turn'}
        await self.submit('/goal_edit 先验证，再完成实验')
        self.assertEqual(self.goal['status'],'active')
        self.assertEqual(self.goal['objective'],'先验证，再完成实验')
        self.assertEqual(self.bridge.jobs.qsize(),0)

    async def test_edit_paused_goal_stays_paused(self):
        self.existing('paused')
        await self.submit('/goal_edit 修改后的目标')
        self.assertEqual(self.goal['status'],'paused')
        self.assertIn('保持暂停',self.bridge.reply.await_args.args[1])

    async def test_pause_and_resume_preserve_goal_and_usage_without_inventing_budget(self):
        self.existing()
        await self.submit('/goal_pause')
        self.assertEqual(self.api_calls[-1],('thread/goal/set',{
            'threadId':'selected-thread','origin':'user','status':'paused'}))
        self.assertEqual(self.goal['tokensUsed'],8400)
        await self.submit('/goal_resume')
        self.assertEqual(self.api_calls[-1],('thread/goal/set',{
            'threadId':'selected-thread','origin':'user','status':'active'}))
        self.assertEqual(self.goal['objective'],'原目标')
        self.assertEqual(self.goal['tokensUsed'],8400)
        self.assertFalse(any('tokenBudget' in params for method,params in self.api_calls))

    async def test_official_subcommand_forms_and_multiline_objective(self):
        await self.submit('/goal\n完成第一项\n再完成第二项')
        self.assertEqual(self.goal['objective'],'完成第一项\n再完成第二项')
        await self.submit('/goal pause')
        self.assertEqual(self.goal['status'],'paused')
        await self.submit('/goal edit 保留暂停的新目标')
        self.assertEqual(self.goal['status'],'paused')
        await self.submit('/goal resume')
        self.assertEqual(self.goal['status'],'active')

    async def test_clear_removes_native_goal(self):
        self.existing()
        await self.submit('/goal_clear')
        self.assertIsNone(self.goal)
        self.assertEqual(self.api_calls[-1],('thread/goal/clear',{'threadId':'selected-thread','origin':'user'}))

    async def test_completed_goal_is_not_restarted_by_resume(self):
        self.existing('complete')
        await self.submit('/goal_resume')
        self.assertEqual(self.goal['status'],'complete')
        self.assertEqual([method for method,params in self.api_calls],['thread/goal/get'])

    async def test_idempotent_pause_and_resume_do_not_trigger_native_mutations(self):
        self.existing('paused')
        await self.submit('/goal_pause')
        self.assertEqual([method for method,params in self.api_calls],['thread/goal/get'])
        self.api_calls.clear();self.goal['status']='active'
        await self.submit('/goal_resume')
        self.assertEqual([method for method,params in self.api_calls],['thread/goal/get'])

    async def test_view_and_missing_goal_actions_never_create_goal(self):
        for command in ['/goal','/goal_edit 新目标','/goal_pause','/goal_resume','/goal_clear']:
            await self.submit(command)
        self.assertIsNone(self.goal)
        self.assertFalse(any(method!='thread/goal/get' for method,params in self.api_calls))

    async def test_empty_or_overlong_objective_is_rejected_before_any_rpc(self):
        for command in ['/goal_edit','/goal set','/goal '+('测'*4001),'/goal_pause 多余参数']:
            await self.submit(command)
        self.assertFalse(self.api_calls)

    async def test_unauthorized_user_cannot_mutate_goal(self):
        await self.submit('/goal 替换别人的目标',user='stranger')
        self.assertFalse(self.api_calls)

    async def test_fresh_chat_can_create_goal_without_a_separate_task(self):
        self.bridge.state.new_thread('chat')
        await self.submit('/goal 新会话目标')
        self.assertEqual(self.bridge.state.thread('chat'),'created-thread')
        self.assertEqual(self.goal['threadId'],'created-thread')
        self.assertEqual(self.goal['status'],'active')
        self.assertEqual(self.bridge.jobs.qsize(),0)

    async def test_goal_notifications_update_cache_and_suppress_routine_turn_start_card(self):
        self.existing()
        await self.submit('/goal_edit 持续验证')
        self.bridge.reply.reset_mock()
        await self.bridge.on_codex({'method':'thread/goal/updated','params':{
            'threadId':'selected-thread','goal':dict(self.goal)}})
        await self.bridge.on_codex({'method':'turn/started','params':{'threadId':'selected-thread',
            'turn':{'id':'goal-turn','status':'inProgress'}}})
        self.bridge.reply.assert_not_called()
        await self.bridge.on_codex({'method':'turn/completed','params':{'threadId':'selected-thread',
            'turn':{'id':'goal-turn','status':'completed','items':[{'type':'agentMessage','phase':'final_answer','text':'已完成第一步'}]}}})
        self.assertIn('目标进展',self.bridge.reply.await_args.args[1])
        self.assertIn('已完成第一步',self.bridge.reply.await_args.args[1])

    async def test_two_simultaneous_initializers_create_only_one_thread(self):
        self.bridge.state.new_thread('chat')
        first,second=await asyncio.gather(self.bridge.prepare_chat_thread('chat'),self.bridge.prepare_chat_thread('chat'))
        self.assertEqual(first,second)
        self.assertEqual(sum(method=='thread/start' for method,params in self.api_calls),1)

    def test_goal_shortcuts_are_registered_in_slash_panel_catalog_and_dispatch(self):
        spec=importlib.util.spec_from_file_location('goal_slash_commands',Path(__file__).with_name('sync_commands.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module.ROOT=Path(__file__).parent
        module.ensure_supported()
        self.assertTrue({'goal','goal_edit','goal_pause','goal_resume','goal_clear'}
                        .issubset({name for name,description in module.COMMANDS}))


if __name__=='__main__':unittest.main()

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from test_bridge import b, config
from message_feedback import MessageFeedback


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state=b.State(Path(self.tmp.name)/'state.sqlite3')
        self.addCleanup(self.state.db.close)
        self.feishu=AsyncMock()
        self.feishu.add_reaction.side_effect=lambda message,emoji: 'reaction-'+emoji
        self.feedback=MessageFeedback(self.feishu,self.state)
        self.addAsyncCleanup(self.feedback.close)

    async def drain(self, feedback=None):
        feedback=feedback or self.feedback
        while feedback.tasks:
            await asyncio.gather(*list(feedback.tasks.values()))
            await asyncio.sleep(0)

    async def test_typing_is_replaced_with_done_on_original_message(self):
        await self.feedback.set('original-user-message','busy')
        await self.drain()
        await self.feedback.set('original-user-message','completed')
        await self.drain()
        self.assertEqual([c.args for c in self.feishu.add_reaction.await_args_list],
                         [('original-user-message','Typing'),('original-user-message','DONE')])
        self.feishu.delete_reaction.assert_awaited_once_with('original-user-message','reaction-Typing')
        self.assertFalse(self.feedback.pending())

    async def test_duplicate_running_updates_do_not_add_duplicate_reactions(self):
        for status in ['busy','running','busy']:
            await self.feedback.set('message',status)
            await self.drain()
        self.feishu.add_reaction.assert_awaited_once_with('message','Typing')

    async def test_status_updates_do_not_block_task_work_and_handle_completion_during_busy_request(self):
        release=asyncio.Event()
        async def add(message,emoji):
            if emoji=='Typing':await release.wait()
            return 'reaction-'+emoji
        self.feishu.add_reaction.side_effect=add
        await self.feedback.set('message','busy')
        await asyncio.sleep(0)
        await asyncio.wait_for(self.feedback.set('message','completed'),0.1)
        self.assertFalse(release.is_set())
        release.set()
        await self.drain()
        self.assertEqual(self.feedback.row('message')['applied'],'DONE')
        self.feishu.delete_reaction.assert_awaited_once_with('message','reaction-Typing')

    async def test_failure_and_interruption_are_never_marked_done(self):
        for status in ['failed','interrupted']:
            await self.feedback.set(status,'busy')
            await self.drain()
            await self.feedback.set(status,status)
            await self.drain()
            self.assertEqual(self.feedback.row(status)['applied'],'CrossMark')
        self.assertFalse(any(c.args[1]=='DONE' for c in self.feishu.add_reaction.await_args_list))

    async def test_delete_failure_is_persisted_and_retried_after_restart(self):
        await self.feedback.set('message','busy')
        await self.drain()
        self.feishu.delete_reaction.side_effect=RuntimeError('temporary disconnect')
        await self.feedback.set('message','completed')
        await self.drain()
        self.assertEqual(json.loads(self.feedback.row('message')['stale_ids']),['reaction-Typing'])
        self.assertEqual(self.feedback.row('message')['applied'],'DONE')
        self.feishu.delete_reaction.side_effect=None
        restored=MessageFeedback(self.feishu,self.state)
        self.addAsyncCleanup(restored.close)
        await restored.reconcile('message')
        self.assertEqual(restored.row('message')['stale_ids'],'[]')
        self.assertEqual(self.feishu.add_reaction.await_count,2)

    async def test_permission_failure_does_not_fail_the_task_and_can_be_reconciled(self):
        self.feishu.add_reaction.side_effect=RuntimeError('Feishu reaction code=99991672')
        await self.feedback.set('message','busy')
        await self.drain()
        self.assertIsNone(self.feedback.row('message')['applied'])
        self.assertEqual(self.feedback.pending(),['message'])
        self.feishu.add_reaction.side_effect=lambda message,emoji:'reaction-'+emoji
        await self.feedback.reconcile('message')
        self.assertEqual(self.feedback.row('message')['applied'],'Typing')

    async def test_restart_uses_saved_task_outcome_to_clear_old_busy_reaction(self):
        self.state.receive({'message_id':'message','chat_id':'chat','user_id':'owner','chat_type':'p2p',
                            'text':'task','created':time.time()})
        await self.feedback.set('message','busy')
        await self.drain()
        self.state.mark('message','interrupted')
        self.feedback.recover()
        await self.feedback.reconcile('message')
        self.assertEqual(self.feedback.row('message')['applied'],'CrossMark')
        self.feishu.delete_reaction.assert_awaited_once_with('message','reaction-Typing')


class BridgeFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge=b.Bridge(config(),Path(self.tmp.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply=AsyncMock(return_value=True)
        self.bridge.codex=AsyncMock()
        self.bridge.feedback=AsyncMock()
        self.bridge.state.bind('owner')

    async def submit(self):
        message={'message_id':'original-message','chat_id':'chat','user_id':'owner','chat_type':'p2p',
                 'text':'检查一下目前状态','created':time.time()}
        self.bridge.state.receive(message)
        await self.bridge.handle(message)
        return message

    async def run_worker(self):
        task=asyncio.create_task(self.bridge.worker())
        try:await asyncio.wait_for(self.bridge.jobs.join(),2)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):await task

    async def test_receiving_task_sets_busy_without_sending_acknowledgement_card(self):
        await self.submit()
        self.bridge.reply.assert_not_called()
        self.bridge.feedback.set.assert_awaited_once_with('original-message','busy')
        self.assertEqual(self.bridge.jobs.qsize(),1)

    async def test_completed_task_sends_only_result_and_marks_original_message_done(self):
        async def request(method,params):
            if method=='thread/start':return {'thread':{'id':'new-thread','cwd':'/tmp'}}
            if method=='turn/start':
                self.bridge.active['final']='这是实际结果'
                self.bridge.active['done'].set_result({'id':'turn','status':'completed'})
                return {'turn':{'id':'turn'}}
            raise AssertionError(method)
        self.bridge.codex.request.side_effect=request
        await self.submit()
        await self.run_worker()
        self.bridge.reply.assert_awaited_once_with('chat','这是实际结果', notice='已完成')
        self.bridge.feedback.set.assert_awaited_with('original-message','completed')

    async def test_task_failure_sends_error_and_marks_failure(self):
        self.bridge.codex.request.side_effect=RuntimeError('backend unavailable')
        await self.submit()
        await self.run_worker()
        self.bridge.reply.assert_awaited_once()
        self.assertIn('任务未能完成',self.bridge.reply.await_args.args[1])
        self.bridge.feedback.set.assert_awaited_with('original-message','failed')


class ReactionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def transport(self,handler):
        client=b.Feishu(config())
        await client.client.aclose()
        client.client=b.httpx.AsyncClient(base_url='https://open.feishu.cn',transport=b.httpx.MockTransport(handler))
        self.addAsyncCleanup(client.client.aclose)
        client.authenticate=AsyncMock(return_value='private-token')
        return client

    async def test_real_request_shapes_and_encoded_reaction_id(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return b.httpx.Response(200,json={'code':0,'data':{'reaction_id':'reaction/+id'}})
        client=await self.transport(handler)
        identifier=await client.add_reaction('om_original','Typing')
        await client.delete_reaction('om_original',identifier)
        self.assertEqual(json.loads(seen[0].content),{'reaction_type':{'emoji_type':'Typing'}})
        self.assertEqual(seen[0].method,'POST')
        self.assertEqual(seen[1].method,'DELETE')
        self.assertTrue(seen[1].url.raw_path.endswith(b'reaction%2F%2Bid'))

    async def test_missing_permission_is_reported_without_retrying(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return b.httpx.Response(400,json={'code':99991672,'msg':'scope required'})
        client=await self.transport(handler)
        with self.assertRaisesRegex(RuntimeError,'99991672'):await client.add_reaction('message','Typing')
        self.assertEqual(len(seen),1)

    async def test_deleting_an_already_removed_reaction_is_successful(self):
        client=await self.transport(lambda req:b.httpx.Response(400,json={'code':231011}))
        await client.delete_reaction('message','old-id')


if __name__=='__main__':unittest.main()

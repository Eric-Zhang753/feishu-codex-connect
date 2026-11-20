import asyncio
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from test_bridge import b, config
from global_questions import ASYNC, BUILTIN, GlobalQuestions, Rollout, question_id
from ide_ipc import IdeIpc


def call(identifier='call-real', name=ASYNC, questions=None):
    return {'type': 'response_item', 'timestamp': '2026-10-30T06:00:00Z', 'payload': {
        'type': 'function_call', 'name': name, 'call_id': identifier,
        'arguments': json.dumps({'questions': questions or [
            {'title': '使用哪个方案？', 'options': ['方案 A', '方案 B']} ]})}}


def user(text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                               'content': [{'type': 'input_text', 'text': text}]}}


def append(path, records):
    with path.open('a') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')


class GlobalQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The scanner is tested with temporary files; no worker thread is needed.
        async def scan_inline(func, *args, **kwargs):
            return func(*args, **kwargs)
        patcher = patch('global_questions.asyncio.to_thread', scan_inline)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        directory = root / 'bridge'
        directory.mkdir(mode=0o700)
        self.sessions = root / 'sessions'
        self.sessions.mkdir()
        self.bridge = b.Bridge(dict(config(), shared_socket='/tmp/shared.sock'), directory)
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.feishu.send_cards = AsyncMock(return_value=['real-card'])
        self.bridge.reply = AsyncMock(return_value=True)
        self.bridge.codex = AsyncMock()
        self.bridge.state.bind('owner')
        self.bridge.state.receive({'message_id': 'owner-message', 'chat_id': 'private-chat', 'user_id': 'owner',
            'chat_type': 'p2p', 'text': '/status', 'created': time.time()})
        self.bridge.state.select_thread('private-chat', 'different-selected-session', '/tmp')
        self.ipc = AsyncMock()
        self.ipc.owner.return_value = 'real-ide-owner'
        self.monitor = GlobalQuestions(self.bridge, self.sessions, self.ipc)

    def session(self, name='thread-a', records=None):
        path = self.sessions / (name + '.jsonl')
        append(path, [{'type': 'session_meta', 'payload': {'id': name}}, user('新任务'),
                      {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'real-turn'}},
                      *(records or [call()])])
        return path

    async def test_real_async_calls_in_all_sessions_notify_without_switching_selected_chat(self):
        self.session('thread-a')
        self.session('thread-b', [call('call-other')])
        await self.monitor.step()
        self.assertEqual(len(self.bridge.approvals), 2)
        self.assertEqual(self.bridge.feishu.send_cards.await_count, 2)
        self.assertEqual(self.bridge.state.thread('private-chat'), 'different-selected-session')
        self.bridge.codex.respond.assert_not_called()
        await self.monitor.step()
        self.assertEqual(self.bridge.feishu.send_cards.await_count, 2)

    async def test_choice_returns_ide_async_reply_with_original_call_and_question_identity(self):
        self.session()
        await self.monitor.step()
        code = next(iter(self.bridge.approvals))
        await self.bridge.answer_text('private-chat', code, '2')
        self.ipc.reply_async.assert_awaited_once_with('thread-a', [{
            'questionItemId': '["request_user_input_async","call-real",0]',
            'question': '使用哪个方案？', 'answer': '方案 B'}])
        self.bridge.codex.respond.assert_not_called()
        self.assertEqual(self.monitor.saved('thread-a','call-real')['status'], 'answered')

    async def test_async_accepted_tool_result_does_not_close_question(self):
        self.session(records=[call(), {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'call_id': 'call-real', 'output': '{"accepted":true}'}}])
        await self.monitor.step()
        self.assertEqual(len(self.bridge.approvals), 1)

    async def test_desktop_answer_clears_phone_card_without_forwarding_again(self):
        path = self.session()
        await self.monitor.step()
        append(path, [user('<send_user_message_question_reply>\n' + json.dumps([{
            'questionItemId': question_id('call-real',0), 'question': '使用哪个方案？', 'answer': '方案 A'}])
            + '\n</send_user_message_question_reply>')])
        await self.monitor.step()
        self.assertFalse(self.bridge.approvals)
        self.ipc.reply_async.assert_not_called()

    async def test_past_answered_or_superseded_questions_are_not_sent_on_startup(self):
        self.session(records=[call(), user('开始新的任务')])
        await self.monitor.step()
        self.bridge.feishu.send_cards.assert_not_called()

    async def test_restore_pending_code_after_restart_preserves_existing_buttons_without_duplicate_card(self):
        self.session()
        await self.monitor.step()
        code = next(iter(self.bridge.approvals))
        self.bridge.approvals.clear()
        restarted = GlobalQuestions(self.bridge, self.sessions, self.ipc)
        await restarted.step()
        self.assertEqual(next(iter(self.bridge.approvals)), code)
        self.bridge.feishu.send_cards.assert_awaited_once()
        await self.bridge.answer_text('private-chat', code, '1')
        self.ipc.reply_async.assert_awaited_once()

    async def test_offline_owner_does_not_send_unanswerable_card_and_is_retried(self):
        self.session()
        self.ipc.owner.side_effect = RuntimeError('no-client-found')
        await self.monitor.step()
        self.bridge.feishu.send_cards.assert_not_called()
        self.ipc.owner.side_effect = None
        await self.monitor.step()
        self.bridge.feishu.send_cards.assert_awaited_once()

    async def test_partial_file_record_is_read_once_when_completed(self):
        path = self.session(records=[user('初始')])
        await self.monitor.step()
        record = json.dumps(call(), ensure_ascii=False)
        with path.open('a') as stream:
            stream.write(record[:40])
        await self.monitor.step()
        self.bridge.feishu.send_cards.assert_not_called()
        with path.open('a') as stream:
            stream.write(record[40:] + '\n')
        # Some mounted filesystems retain the same mtime for rapid appends.
        previous_mtime = self.monitor.rollouts[path].modified
        os.utime(path, ns=(previous_mtime, previous_mtime))
        await self.monitor.step()
        self.bridge.feishu.send_cards.assert_awaited_once()

    async def test_builtin_question_uses_actual_request_id_from_owning_ide(self):
        original = [{'id': 'preference', 'header': '选择', 'question': '哪个方案？',
                     'options': [{'label': '方案 A','description': '第一项'}]}]
        self.session(records=[call(name=BUILTIN, questions=original)])
        actual = {'id': 88, 'method': 'item/tool/requestUserInput', 'params': {
            'threadId': 'thread-a', 'turnId': 'real-turn', 'itemId': 'call-real', 'questions': original}}
        self.ipc.snapshot.return_value = ('owner', {'requests': [actual]})
        await self.monitor.step()
        code = next(iter(self.bridge.approvals))
        await self.bridge.answer_text('private-chat', code, '1')
        self.ipc.reply_builtin.assert_awaited_once_with('thread-a',88,{
            'answers': {'preference': {'answers':['方案 A']}}})

    async def test_failed_delivery_remains_retryable(self):
        self.session()
        self.bridge.feishu.send_cards.side_effect = RuntimeError('network down')
        self.bridge.reply.return_value = False
        await self.monitor.step()
        self.assertFalse(self.monitor.registered)
        self.bridge.feishu.send_cards.side_effect = None
        await self.monitor.step()
        self.assertEqual(len(self.monitor.registered),1)


class IpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'ipc.sock'
        self.messages = []
        self.steer_error = None
        async def handle(reader,writer):
            try:
                while True:
                    size = struct.unpack('<I',await reader.readexactly(4))[0]
                    message = json.loads(await reader.readexactly(size))
                    self.messages.append(message)
                    if message['type'] != 'request':continue
                    method = message['method']
                    reply = {'type':'response','requestId':message['requestId'], 'resultType':'success',
                             'handledByClientId':'owner','method':method,'result': {'ok':True}}
                    if method=='initialize':reply['result']={'clientId':'feishu-client'}
                    elif method=='thread-owner-discovery':reply['result']={'supportsUntrustedAppInput':True}
                    elif method=='thread-follower-steer-turn' and self.steer_error:
                        reply.update(resultType='error',error=self.steer_error)
                    data=json.dumps(reply).encode();writer.write(struct.pack('<I',len(data))+data)
                    await writer.drain()
            except asyncio.IncompleteReadError:pass
            finally:writer.close()
        self.server=await asyncio.start_unix_server(handle,str(self.path))
        self.path.chmod(0o600)
        async def close_server():
            self.server.close()
            await self.server.wait_closed()
        self.addAsyncCleanup(close_server)
        self.client=IdeIpc(self.tmp.name,self.path)
        self.addAsyncCleanup(self.client.close)

    async def test_async_answer_uses_private_ipc_and_exact_ui_reply_envelope(self):
        replies=[{'questionItemId':question_id('call-x',0),'question':'原问题','answer':'选项'}]
        await self.client.reply_async('real-thread',replies)
        message=self.messages[-1]
        self.assertEqual(message['method'],'thread-follower-steer-turn')
        self.assertEqual(message['targetClientId'],'owner')
        self.assertEqual(message['params']['conversationId'],'real-thread')
        text=message['params']['input'][0]['text']
        self.assertEqual(json.loads(text.split('\n')[1]),replies)

    async def test_only_explicit_idle_rejection_can_fall_back_to_starting_turn(self):
        self.steer_error='Conversation real-thread is not being streamed.'
        await self.client.reply_async('real-thread',[])
        message=self.messages[-1]
        self.assertEqual(message['method'],'thread-follower-start-turn')
        self.assertEqual(message['version'],2)
        self.assertEqual(message['params']['turnStart']['request']['threadId'],'real-thread')

    async def test_ambiguous_delivery_error_is_not_retried_as_new_turn(self):
        self.steer_error='request-timeout'
        with self.assertRaisesRegex(RuntimeError,'request-timeout'):
            await self.client.reply_async('real-thread',[])
        self.assertFalse(any(x['method']=='thread-follower-start-turn' for x in self.messages))

    async def test_actual_ide_turn_ended_error_continues_the_original_conversation(self):
        self.steer_error='Cannot steer conversation real-thread because its active turn already ended'
        await self.client.reply_async('real-thread',[])
        self.assertEqual(self.messages[-1]['method'],'thread-follower-start-turn')
        self.assertEqual(self.messages[-1]['params']['conversationId'],'real-thread')


if __name__=='__main__':unittest.main()

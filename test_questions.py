import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import tempfile
import time
import sys
import unittest
from unittest.mock import AsyncMock

from test_bridge import b, config
from question_channel import QuestionClient, QuestionServer, private_directory


def request(identifier=7, thread='ide-thread', count=1):
    return {'id': identifier, 'method': 'item/tool/requestUserInput', 'params': {
        'threadId': thread, 'turnId': 'turn-1', 'itemId': 'item-1', 'isBlocking': False,
        'questions': [{'id': 'q' + str(i), 'header': '选择', 'question': '请选择方案 ' + str(i),
                       'options': [{'label': '方案 A', 'description': '第一项'},
                                   {'label': '方案 B', 'description': '第二项'}]} for i in range(count)]}}


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = b.Bridge(dict(config(), shared_socket='/tmp/shared.sock'), Path(self.tmp.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply = AsyncMock()
        self.bridge.feishu.send_cards = AsyncMock(return_value=['message'])
        self.bridge.codex = AsyncMock()
        self.bridge.loop = asyncio.get_running_loop()
        self.bridge.state.bind('owner')
        self.bridge.state.receive({'message_id': 'original', 'chat_id': 'private-chat',
            'user_id': 'owner', 'chat_type': 'p2p', 'text': '/status', 'created': time.time()})
        self.bridge.state.select_thread('private-chat', 'some-other-selected-thread', '/tmp')
        self.respond = AsyncMock()
        self.source = object()

    async def register(self, event=None):
        await self.bridge.relay_question(event or request(), self.respond, self.source)
        return next(iter(self.bridge.approvals))

    async def answer(self, code, text, chat='private-chat', user='owner'):
        msg = {'message_id': str(time.time_ns()), 'chat_id': chat, 'user_id': user,
               'chat_type': 'p2p', 'text': '/answer ' + code + ' ' + text, 'created': time.time()}
        self.bridge.state.receive(msg)
        await self.bridge.handle(msg)

    async def test_unselected_ide_thread_routes_numbered_answer_to_original_connection(self):
        code = await self.register()
        self.assertIsNone(self.bridge.active)
        self.assertEqual(self.bridge.feishu.send_cards.await_args.args[0], 'private-chat')
        await self.answer(code, '2')
        self.respond.assert_awaited_once_with(7, {'answers': {'q0': {'answers': ['方案 B']}}})
        self.bridge.codex.respond.assert_not_called()
        self.assertFalse(self.bridge.approvals)

    async def test_multi_question_numbered_and_json_answers(self):
        code = await self.register(request(count=2))
        await self.answer(code, '2;1')
        self.respond.assert_awaited_with(7, {'answers': {
            'q0': {'answers': ['方案 B']}, 'q1': {'answers': ['方案 A']}}})
        code = await self.register(request(identifier=8, count=2))
        await self.answer(code, '{"q0":"1","q1":"自定义内容"}')
        self.respond.assert_awaited_with(8, {'answers': {
            'q0': {'answers': ['方案 A']}, 'q1': {'answers': ['自定义内容']}}})

    async def test_bad_input_foreign_chat_and_foreign_user_cannot_answer(self):
        code = await self.register()
        for text, chat, user in [('99', 'private-chat', 'owner'), ('', 'private-chat', 'owner'),
                                  ('1', 'another-chat', 'owner'), ('1', 'private-chat', 'stranger')]:
            await self.answer(code, text, chat, user)
        self.respond.assert_not_called()
        self.assertIn(code, self.bridge.approvals)

    async def test_free_text_and_question_without_description(self):
        event = request()
        event['params']['questions'][0]['options'] = None
        code = await self.register(event)
        await self.answer(code, '下午再处理')
        self.respond.assert_awaited_with(7, {'answers': {'q0': {'answers': ['下午再处理']}}})

    async def test_duplicate_request_is_displayed_once_and_uses_ide_source(self):
        await self.bridge.register_question(request(), 'private-chat')
        code = next(iter(self.bridge.approvals))
        await self.register()
        self.bridge.feishu.send_cards.assert_awaited_once()
        await self.answer(code, '1')
        self.respond.assert_awaited_once()

    async def test_resolution_is_scoped_to_thread_and_stale_answer_is_ignored(self):
        first = await self.register()
        second = await self.register(request(thread='another-thread'))
        self.bridge.resolve_question({'threadId': 'ide-thread', 'requestId': 7})
        self.assertNotIn(first, self.bridge.approvals)
        self.assertEqual(len(self.bridge.approvals), 1)
        await self.answer(first, '1')
        self.respond.assert_not_called()

    async def test_buttons_collect_every_question_before_submitting_and_deduplicate(self):
        code = await self.register(request(count=2))
        def click(q, option):
            return {'_card': True, 'user_id': 'owner', 'chat_id': 'private-chat',
                    'value': {'code': code, 'question': q, 'option': option}}
        await self.bridge.handle_card(click('q0', 2))
        self.respond.assert_not_called()
        await self.bridge.handle_card(click('q1', 1))
        await self.bridge.handle_card(click('q1', 1))
        self.respond.assert_awaited_once_with(7, {'answers': {
            'q0': {'answers': ['方案 B']}, 'q1': {'answers': ['方案 A']}}})

    async def test_callback_checks_owner_chat_question_and_option(self):
        code = await self.register()
        raw = {'event': {'operator': {'open_id': 'stranger'},
               'context': {'open_chat_id': 'private-chat'}, 'action': {
               'value': {'action': 'codex_answer', 'code': code, 'question': 'q0', 'option': 1}}}}
        self.assertEqual(self.bridge.receive_card(raw)[1], 'error')
        raw['event']['operator']['open_id'] = 'owner'
        self.assertEqual(self.bridge.receive_card(raw)[1], 'info')
        await asyncio.sleep(0)
        message = await asyncio.wait_for(self.bridge.incoming.get(), 1)
        message['value']['option'] = True
        await self.bridge.handle_card(message)
        self.respond.assert_not_called()
        message['value']['option'] = 1
        await self.bridge.handle_card(message)
        self.respond.assert_awaited_once()

    async def test_secret_questions_are_kept_in_ide(self):
        event = request()
        event['params']['questions'][0]['isSecret'] = True
        await self.bridge.relay_question(event, self.respond, self.source)
        self.assertFalse(self.bridge.approvals)
        self.bridge.feishu.send_cards.assert_not_called()

    async def test_card_contains_buttons_fallback_command_and_redacts_credentials(self):
        event = request()
        event['params']['questions'][0]['question'] = config()['app_secret']
        code = await self.register(event)
        cards = self.bridge.feishu.send_cards.await_args.args[1]
        rendered = json.dumps(cards, ensure_ascii=False)
        self.assertNotIn(config()['app_secret'], rendered)
        self.assertIn('/answer ' + code, rendered)
        buttons = [e for c in cards for e in c['body']['elements'] if e['tag'] == 'button']
        self.assertEqual([e['behaviors'][0]['value']['option'] for e in buttons], [1, 2])

    async def test_card_failure_falls_back_to_text_with_numbered_options(self):
        self.bridge.feishu.send_cards.side_effect = RuntimeError('unsupported card')
        code = await self.register()
        text = self.bridge.reply.await_args.args[1]
        self.assertIn('/answer ' + code, text)
        self.assertIn('2. 方案 B', text)

    async def test_socket_round_trip_and_ide_resolution(self):
        path = Path(self.tmp.name) / 'channel' / 'question.sock'
        server = QuestionServer(self.bridge, path)
        await server.start()
        self.addAsyncCleanup(server.close)
        upstream = AsyncMock()
        client = QuestionClient(upstream, path)
        self.addAsyncCleanup(client.close)
        self.assertTrue(await client.observe(request()))
        for _ in range(100):
            if self.bridge.approvals:
                break
            await asyncio.sleep(0.01)
        code = next(iter(self.bridge.approvals))
        await self.answer(code, '2')
        for _ in range(100):
            if upstream.await_count:
                break
            await asyncio.sleep(0.01)
        upstream.assert_awaited_once_with({'id': 7, 'result': {'answers': {'q0': {'answers': ['方案 B']}}}})
        await client.observe(request(identifier=8))
        await asyncio.sleep(0.02)
        await client.observe({'method': 'serverRequest/resolved', 'params': {'threadId': 'ide-thread', 'requestId': 8}})
        await asyncio.sleep(0.02)
        self.assertFalse(self.bridge.approvals)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(RuntimeError):
            await QuestionServer(self.bridge, path).start()

    async def test_missing_channel_does_not_break_ide(self):
        client = QuestionClient(AsyncMock(), Path(self.tmp.name) / 'missing' / 'bridge.sock')
        self.assertFalse(await client.observe(request()))
        await client.close()

    async def test_real_ide_proxy_preserves_async_server_request_and_returns_feishu_answer(self):
        from websockets.asyncio.server import unix_serve
        path = Path(self.tmp.name) / 'channel' / 'question.sock'
        server = QuestionServer(self.bridge, path)
        await server.start()
        self.addAsyncCleanup(server.close)
        backend_socket = Path(self.tmp.name) / 'backend.sock'
        received = asyncio.get_running_loop().create_future()
        original = request()
        async def backend(ws):
            await ws.send(json.dumps(original))
            received.set_result(json.loads(await ws.recv()))
        async with unix_serve(backend, str(backend_socket)):
            program = ('import sys,asyncio; from pathlib import Path; '
                       'sys.path.insert(0,sys.argv[1]); import shared_backend; '
                       'shared_backend.ROOT=Path(sys.argv[3]).parent; '
                       'asyncio.run(shared_backend.relay_stdio(sys.argv[2]))')
            process = await asyncio.create_subprocess_exec(sys.executable, '-c', program,
                str(Path(__file__).parent), str(backend_socket), str(path),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                displayed = json.loads(await asyncio.wait_for(process.stdout.readline(), 3))
                self.assertTrue(displayed['params']['isBlocking'])
                self.assertFalse(original['params']['isBlocking'])
                for _ in range(100):
                    if self.bridge.approvals:
                        break
                    await asyncio.sleep(0.01)
                code = next(iter(self.bridge.approvals))
                await self.answer(code, '2')
                self.assertEqual(await asyncio.wait_for(received, 3), {
                    'id': 7, 'result': {'answers': {'q0': {'answers': ['方案 B']}}}})
                await asyncio.wait_for(process.wait(), 3)
                self.assertEqual(process.returncode, 0, (await process.stderr.read()).decode())
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()

    def test_question_directory_rejects_symlinks_and_public_access(self):
        path = Path(self.tmp.name) / 'public'
        path.mkdir(mode=0o755)
        path.chmod(0o755)
        with self.assertRaises(RuntimeError):
            private_directory(path)
        link = Path(self.tmp.name) / 'link'
        link.symlink_to(Path(self.tmp.name), target_is_directory=True)
        with self.assertRaises(RuntimeError):
            private_directory(link)


if __name__ == '__main__':
    unittest.main(verbosity=2)

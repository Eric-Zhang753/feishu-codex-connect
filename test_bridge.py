import asyncio
from contextlib import suppress
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).with_name('bridge.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def config():
    return {'app_id': 'cli_test', 'app_secret': 'private-test-secret',
            'project_dir': '/tmp', 'bot_open_id': 'bot-id', 'allowed_user_ids': [],
            'message_feedback_enabled': False}


def event(**changes):
    message = {'message_id': 'msg-1', 'chat_id': 'chat-1', 'chat_type': 'p2p',
               'message_type': 'text', 'create_time': str(int(time.time() * 1000)),
               'content': json.dumps({'text': 'run the experiment'})}
    message.update(changes)
    return {'header': {'app_id': 'cli_test'}, 'event': {
        'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'owner'}}, 'message': message}}


class EventTests(unittest.TestCase):
    def test_direct_text_normalization(self):
        msg = b.normalize_event(event(), config())
        self.assertEqual(msg['user_id'], 'owner')
        self.assertEqual(msg['text'], 'run the experiment')

    def test_bot_sender_and_foreign_app_are_rejected(self):
        raw = event()
        raw['event']['sender']['sender_type'] = 'app'
        self.assertIsNone(b.normalize_event(raw, config()))
        raw = event()
        raw['header']['app_id'] = 'different-app'
        self.assertIsNone(b.normalize_event(raw, config()))

    def test_group_requires_mention_of_this_bot(self):
        self.assertIsNone(b.normalize_event(event(chat_type='group'), config()))
        mention = {'key': '@_user_1', 'id': {'open_id': 'bot-id'}}
        msg = b.normalize_event(event(chat_type='group', mentions=[mention],
            content=json.dumps({'text': '@_user_1 run'})), config())
        self.assertEqual(msg['text'], 'run')
        mention['id']['open_id'] = 'another-bot'
        self.assertIsNone(b.normalize_event(event(chat_type='group', mentions=[mention]), config()))

    def test_stale_messages_and_attachments_are_rejected(self):
        self.assertIsNone(b.normalize_event(event(create_time='1000'), config()))
        self.assertIsNone(b.normalize_event(event(message_type='image'), config()))

    def test_credentials_and_mentions_are_redacted(self):
        text = b.clean('private-test-secret Bearer token123 <at user_id="all">all</at>', config())
        self.assertNotIn('private-test-secret', text)
        self.assertNotIn('token123', text)
        self.assertNotIn('<at ', text)


class CardTests(unittest.TestCase):
    def test_markdown_is_preserved_in_native_cards(self):
        text = '## 标题\n\n**粗体**、*斜体*和 `code`\n\n- 一\n- 二\n'
        cards = b.message_cards(text, config())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['schema'], '2.0')
        self.assertEqual(cards[0]['header']['title']['tag'], 'plain_text')
        self.assertEqual(cards[0]['body']['elements'], [{'tag': 'markdown', 'content': text}])

    def test_empty_and_control_character_messages_are_displayable(self):
        self.assertEqual(b.message_cards('', config())[0]['body']['elements'][0]['content'], '（无正文）')
        content = b.message_cards('a\x00b\r\nc\rd\t!', config())[0]['body']['elements'][0]['content']
        self.assertEqual(content, 'ab\nc\nd\t!')

    def test_long_plain_line_is_complete_and_bounded(self):
        text = '中文🙂\\"' * 2300
        chunks = b.split_card_markdown(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(0 < len(part) <= 3600 for part in chunks))

    def test_code_chunks_keep_fences_and_language(self):
        for marker in ('```', '~~~', '````'):
            with self.subTest(marker=marker):
                lines = [f'print("unique-{i:04d}")\n' for i in range(600)]
                text = marker + 'python\n' + ''.join(lines) + marker
                chunks = b.split_card_markdown(text)
                self.assertGreater(len(chunks), 1)
                self.assertTrue(all(len(part) <= 3600 for part in chunks))
                for part in chunks:
                    self.assertTrue(part.startswith(marker + 'python\n'))
                    self.assertTrue(part.endswith(marker))
                combined = ''.join(chunks)
                for line in lines:
                    self.assertEqual(combined.count(line), 1)

    def test_long_code_line_keeps_all_data_between_fences(self):
        line = 'A' * 13000
        chunks = b.split_card_markdown('```text\n' + line + '\n```')
        self.assertTrue(all(len(part) <= 3600 for part in chunks))
        restored = ''.join(part.removeprefix('```text\n').removesuffix('```').rstrip('\n') for part in chunks)
        self.assertEqual(restored, line)

    def test_table_headers_repeat_and_rows_are_not_lost(self):
        header = '| 名称 | 说明 |\n| --- | --- |\n'
        rows = [f'| unique-{i:04d} | 测试内容内容内容 |\n' for i in range(400)]
        chunks = b.split_card_markdown(header + ''.join(rows))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(part.startswith(header) and len(part) <= 3600 for part in chunks))
        combined = ''.join(chunks)
        for row in rows:
            self.assertEqual(combined.count(row), 1)

    def test_boundaries_near_fences_and_headers_remain_bounded(self):
        for offset in range(160):
            text = ('x' * (3500 + offset) + '\n```python\n' + 'print(1)\n' * 450 +
                    '```\n\n| a | b |\n| --- | --- |\n' + '| x | y |\n' * 450 + '\nend')
            chunks = b.split_card_markdown(text)
            self.assertTrue(all(len(part) <= 3600 for part in chunks), offset)
            self.assertEqual(''.join(chunks).count('print(1)'), 450)
            self.assertEqual(''.join(chunks).count('| x | y |'), 450)

    def test_cards_remain_below_payload_limit_with_unicode_and_escapes(self):
        for value in ('🙂', '\\', '"', '\t', '中'):
            with self.subTest(value=value):
                cards = b.message_cards(value * 24000, config())
                for card in cards:
                    content = json.dumps(card, ensure_ascii=False)
                    payload = json.dumps({'receive_id': 'chat-1', 'msg_type': 'interactive',
                                          'uuid': '0' * 36, 'content': content}, ensure_ascii=False)
                    self.assertLess(len(payload.encode()), 20000)

    def test_redaction_and_numbering_apply_to_every_card(self):
        text = '**你好**\n' + 'private-test-secret <at user_id="all">all</at>\n' * 120
        cards = b.message_cards(text, config())
        self.assertGreater(len(cards), 1)
        for i, card in enumerate(cards, 1):
            content = card['body']['elements'][0]['content']
            self.assertNotIn('private-test-secret', content)
            self.assertNotIn('<at ', content)
            title = card['header']['title']['content']
            self.assertTrue(title.endswith(f' · {i}/{len(cards)}'))
            self.assertNotIn('private-test-secret', title)
            self.assertNotIn('<at ', title)

    def test_literal_code_uses_longer_delimiters_than_embedded_fences(self):
        command = 'echo "**literal**"\n```\n````'
        text = b.markdown_code(command)
        self.assertEqual(text, '`````text\n' + command + '\n`````')


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bridge = b.Bridge(config(), Path(self.directory.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply = AsyncMock()
        self.bridge.codex = AsyncMock()

    async def submit(self, text, user='owner', chat='chat-1', chat_type='p2p'):
        msg = {'message_id': 'msg-' + str(time.time_ns()), 'chat_id': chat, 'user_id': user,
               'chat_type': chat_type, 'text': text, 'created': time.time()}
        self.bridge.state.receive(msg)
        await self.bridge.handle(msg)
        return msg

    async def test_no_task_can_run_before_binding(self):
        await self.submit('delete all files')
        self.assertEqual(self.bridge.jobs.qsize(), 0)
        self.bridge.codex.request.assert_not_called()

    async def test_pairing_is_private_and_single_owner(self):
        pair = json.loads(self.bridge.pair_path.read_text())
        await self.submit('/bind ' + pair['code'], chat_type='group')
        self.assertIsNone(self.bridge.state.setting('owner'))
        await self.submit('/bind wrong')
        self.assertIsNone(self.bridge.state.setting('owner'))
        await self.submit('/bind ' + pair['code'])
        self.assertEqual(self.bridge.state.setting('owner'), 'owner')
        self.assertFalse(self.bridge.pair_path.exists())
        await self.submit('run another task', user='stranger')
        self.assertEqual(self.bridge.jobs.qsize(), 0)

    async def test_authorized_tasks_are_persisted_then_queued(self):
        self.bridge.state.bind('owner')
        msg = await self.submit('run a reproducible experiment')
        self.assertEqual(self.bridge.jobs.qsize(), 1)
        self.assertEqual(self.bridge.state.rows('queued')[0]['message_id'], msg['message_id'])

    async def test_duplicate_message_id_is_accepted_once(self):
        msg = b.normalize_event(event(), config())
        self.assertTrue(self.bridge.state.receive(msg))
        self.assertFalse(self.bridge.state.receive(msg))

    async def test_approval_is_scoped_to_requesting_chat(self):
        self.bridge.state.bind('owner')
        self.bridge.approvals['abc123'] = {'rpc_id': 99, 'kind': 'command',
                                          'params': {}, 'chat_id': 'chat-1'}
        await self.submit('/approve abc123', chat='another-chat')
        self.bridge.codex.respond.assert_not_called()
        await self.submit('/approve abc123')
        self.bridge.codex.respond.assert_awaited_once_with(99, {'decision': 'accept'})
        self.assertNotIn('abc123', self.bridge.approvals)

    async def test_permissions_are_never_auto_granted(self):
        self.bridge.state.bind('owner')
        self.bridge.active = {'chat_id': 'chat-1', 'thread_id': 'thread-1', 'file_changes': {}}
        await self.bridge.on_codex({'id': 8, 'method': 'item/permissions/requestApproval', 'params': {
            'threadId': 'thread-1', 'permissions': {'network': {'enabled': True}}, 'cwd': '/tmp'}})
        self.bridge.codex.respond.assert_not_called()
        code = next(iter(self.bridge.approvals))
        await self.submit('/deny ' + code)
        self.bridge.codex.respond.assert_awaited_once_with(8, {'permissions': {}, 'scope': 'turn'})

    async def test_approval_card_keeps_shell_commands_literal(self):
        self.bridge.state.bind('owner')
        self.bridge.active = {'chat_id': 'chat-1', 'thread_id': 'thread-1', 'file_changes': {}}
        command = 'echo "**literal**"\n```'
        await self.bridge.on_codex({'id': 8, 'method': 'item/commandExecution/requestApproval', 'params': {
            'threadId': 'thread-1', 'command': command, 'cwd': '/tmp'}})
        code = next(iter(self.bridge.approvals))
        text = self.bridge.reply.await_args.args[1]
        self.assertIn(b.markdown_code(command), text)
        self.assertIn('`/approve ' + code + '`', text)
        self.assertIn('`/deny ' + code + '`', text)
        self.bridge.codex.respond.assert_not_called()

    async def test_unknown_request_cannot_gain_approval(self):
        self.bridge.active = {'chat_id': 'chat-1', 'thread_id': 'thread-1'}
        await self.bridge.on_codex({'id': 4, 'method': 'future/unsafeApproval',
                                    'params': {'threadId': 'thread-1'}})
        self.assertEqual(self.bridge.codex.respond.await_args.kwargs['error']['code'], -32601)

    def thread_data(self, identifier='thread-1'):
        return {'id': identifier, 'name': 'Stored session ' + identifier, 'cwd': self.directory.name,
                'model': 'stored-model', 'modelProvider': 'openai', 'source': 'vscode',
                'updatedAt': int(time.time()), 'status': {'type': 'notLoaded'}}

    def mock_thread_api(self, threads):
        async def request(method, params):
            if method == 'thread/list':
                return {'data': threads, 'nextCursor': None}
            thread = next(t for t in threads if t['id'] == params['threadId'])
            if method == 'thread/read':
                return {'thread': thread}
            if method == 'thread/turns/list':
                return {'data': [{'status': 'completed'}]}
            if method == 'thread/resume':
                return {'thread': thread, 'cwd': thread['cwd'], 'model': thread['model'],
                        'modelProvider': thread['modelProvider']}
            raise AssertionError('Unexpected RPC method: ' + method)
        self.bridge.codex.request.side_effect = request

    async def test_session_numbers_are_stable_even_when_server_order_changes(self):
        self.bridge.state.bind('owner')
        threads = [self.thread_data('thread-1'), self.thread_data('thread-2')]
        self.mock_thread_api(threads)
        await self.submit('/sessions')
        threads.reverse()
        await self.submit('/use 1')
        self.assertEqual(self.bridge.state.thread('chat-1'), 'thread-1')
        calls = self.bridge.codex.request.await_args_list
        self.assertEqual(sum(c.args[0] == 'thread/list' for c in calls), 1)
        resume = next(c.args[1] for c in calls if c.args[0] == 'thread/resume')
        self.assertEqual(resume, {'threadId': 'thread-1', 'excludeTurns': True})
        self.assertFalse(any(c.args[0] == 'turn/start' for c in calls))
        context = self.bridge.state.context('chat-1')
        self.assertEqual(context['cwd'], self.directory.name)
        self.assertEqual(context['model'], 'stored-model')

    async def test_session_pages_keep_distinct_numbers_and_survive_restart(self):
        self.bridge.state.bind('owner')
        pages = [{'data': [self.thread_data(f'thread-{i}') for i in range(1, 11)], 'nextCursor': 'page-2'},
                 {'data': [self.thread_data('thread-11')], 'nextCursor': None}]
        self.bridge.codex.request.side_effect = pages
        await self.submit('/sessions')
        await self.submit('/sessions 2')
        output = self.bridge.reply.await_args.args[1]
        self.assertIn('11. Stored session thread-11', output)
        self.assertEqual(self.bridge.session_snapshot('chat-1')['threads'][10]['id'], 'thread-11')
        reloaded = b.State(Path(self.directory.name) / 'state.sqlite3')
        try:
            snapshot = json.loads(reloaded.setting('session-list:chat-1'))
            self.assertEqual(snapshot['threads'][10]['id'], 'thread-11')
        finally:
            reloaded.db.close()

    async def test_expired_session_number_does_not_switch(self):
        self.bridge.state.bind('owner')
        self.bridge.state.set_setting('session-list:chat-1', json.dumps({
            'created': time.time() - 1900, 'threads': [self.thread_data()]}))
        await self.submit('/use 1')
        self.bridge.codex.request.assert_not_called()
        self.assertIsNone(self.bridge.state.thread('chat-1'))

    async def test_busy_or_queued_robot_cannot_switch_sessions(self):
        self.bridge.state.bind('owner')
        self.bridge.active = {'chat_id': 'another-chat'}
        await self.submit('/use thread-1')
        self.bridge.codex.request.assert_not_called()
        self.bridge.active = None
        await self.submit('a queued task')
        await self.submit('/use thread-1')
        self.bridge.codex.request.assert_not_called()

    async def test_persisted_active_target_is_not_resumed(self):
        self.bridge.state.bind('owner')
        self.bridge.state.thread('chat-1', 'old-thread')
        self.bridge.codex.request.side_effect = [
            {'thread': self.thread_data()}, {'data': [{'status': 'inProgress'}]}]
        with self.assertRaisesRegex(RuntimeError, '仍在执行'):
            await self.submit('/use thread-1')
        self.assertEqual(self.bridge.state.thread('chat-1'), 'old-thread')
        self.assertFalse(any(c.args[0] == 'thread/resume' for c in self.bridge.codex.request.await_args_list))

    async def test_failed_resume_preserves_old_binding(self):
        self.bridge.state.bind('owner')
        self.bridge.state.thread('chat-1', 'old-thread')
        self.bridge.codex.request.side_effect = [
            {'thread': self.thread_data()}, {'data': []}, RuntimeError('resume unavailable')]
        with self.assertRaisesRegex(RuntimeError, 'resume unavailable'):
            await self.submit('/use thread-1')
        self.assertEqual(self.bridge.state.thread('chat-1'), 'old-thread')

    async def test_writer_lock_is_explained_and_preserves_binding(self):
        self.bridge.state.bind('owner')
        self.bridge.state.thread('chat-1', 'old-thread')
        self.bridge.codex.request.side_effect = [
            {'thread': self.thread_data()}, {'data': []}, RuntimeError('thread already has an active writer')]
        with self.assertRaisesRegex(RuntimeError, '另一个 Codex 实例占用'):
            await self.submit('/use thread-1')
        self.assertEqual(self.bridge.state.thread('chat-1'), 'old-thread')

    async def test_current_uses_real_metadata_and_new_preserves_project(self):
        self.bridge.state.bind('owner')
        self.bridge.state.select_thread('chat-1', 'thread-1', self.directory.name, 'stored-model', 'openai')
        thread = self.thread_data()
        thread['model'] = 'actual-model'
        self.mock_thread_api([thread])
        await self.submit('/current')
        self.assertIn('模型：actual-model', self.bridge.reply.await_args.args[1])
        self.assertIn('项目：' + self.directory.name, self.bridge.reply.await_args.args[1])
        await self.submit('/new')
        self.assertIsNone(self.bridge.state.thread('chat-1'))
        self.assertEqual(self.bridge.project_for('chat-1'), self.directory.name)

    async def test_current_without_selection_does_not_create_a_thread(self):
        self.bridge.state.bind('owner')
        await self.submit('/current')
        self.bridge.codex.request.assert_not_called()
        self.assertIn('尚未选择', self.bridge.reply.await_args.args[1])

    async def test_unbound_user_cannot_list_sessions(self):
        await self.submit('/sessions', user='stranger')
        self.bridge.codex.request.assert_not_called()

    async def test_worker_restores_selected_project_and_model_without_overrides(self):
        self.bridge.state.bind('owner')
        self.bridge.state.select_thread('chat-1', 'thread-1', self.directory.name, 'stored-model', 'openai')
        thread = self.thread_data()
        async def request(method, params):
            if method == 'thread/read':
                return {'thread': thread}
            if method == 'thread/turns/list':
                return {'data': [{'status': 'completed'}]}
            if method == 'thread/resume':
                self.assertEqual(params, {'threadId': 'thread-1', 'excludeTurns': True})
                return {'thread': thread, 'cwd': self.directory.name, 'model': 'stored-model', 'modelProvider': 'openai'}
            if method == 'turn/start':
                self.assertEqual(params['threadId'], 'thread-1')
                self.assertNotIn('cwd', params)
                self.assertNotIn('model', params)
                self.bridge.active['final'] = 'done'
                self.bridge.active['done'].set_result({'id': 'turn-1', 'status': 'completed'})
                return {'turn': {'id': 'turn-1'}}
            raise AssertionError(method)
        self.bridge.codex.request.side_effect = request
        await self.submit('continue the selected session')
        task = asyncio.create_task(self.bridge.worker())
        try:
            await asyncio.wait_for(self.bridge.jobs.join(), 3)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.assertEqual(self.bridge.state.rows('completed')[0]['result'], 'done')
        self.assertEqual(self.bridge.state.context('chat-1')['model'], 'stored-model')


class StateMigrationTests(unittest.TestCase):
    def test_old_chat_schema_retains_existing_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE chats (chat_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL)')
                db.execute('INSERT INTO chats VALUES (?,?)', ('existing-chat', 'existing-thread'))
            state = b.State(path)
            try:
                self.assertEqual(state.thread('existing-chat'), 'existing-thread')
                state.thread('existing-chat', 'next-thread')
                self.assertEqual(state.context('existing-chat')['thread_id'], 'next-thread')
            finally:
                state.db.close()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_transport_retries_with_same_uuid_and_numbers_parts(self):
        requests = []
        def handler(request):
            if 'tenant_access_token' in request.url.path:
                return b.httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return b.httpx.Response(200, json={'code': 11232, 'msg': 'rate limited'})
            return b.httpx.Response(200, json={'code': 0, 'data': {'message_id': 'msg-' + str(len(requests))}})
        client = b.Feishu(config())
        await client.client.aclose()
        client.client = b.httpx.AsyncClient(base_url='https://open.feishu.cn', transport=b.httpx.MockTransport(handler))
        try:
            with patch.object(b.asyncio, 'sleep', new=AsyncMock()):
                ids = await client.send('chat-1', '**开头**\n' + 'long response\n' * 500)
            self.assertEqual(len(ids), 2)
            self.assertEqual(requests[0]['uuid'], requests[1]['uuid'])
            self.assertNotEqual(requests[1]['uuid'], requests[2]['uuid'])
            for request in requests:
                self.assertEqual(request['msg_type'], 'interactive')
                self.assertEqual(request['receive_id'], 'chat-1')
                card = json.loads(request['content'])
                self.assertEqual(card['schema'], '2.0')
                self.assertEqual(card['body']['elements'][0]['tag'], 'markdown')
            self.assertIn('**开头**', json.loads(requests[0]['content'])['body']['elements'][0]['content'])
        finally:
            await client.client.aclose()

    async def test_real_subprocess_rpc_handshake_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'fake-codex'
            fake.write_text('#!' + sys.executable + '\n' + '''
import json, sys
for line in sys.stdin:
    message=json.loads(line)
    if 'id' not in message:
        continue
    if message['method']=='initialize':
        reply={'id':message['id'],'result':{}}
    elif message['method']=='test/error':
        reply={'id':message['id'],'error':{'code':-1,'message':'expected failure'}}
    else:
        reply={'id':message['id'],'result':{'echo':message['params']}}
    print(json.dumps(reply), flush=True)
''')
            fake.chmod(0o700)
            client = b.Codex(AsyncMock())
            with patch.object(b, 'codex_binary', return_value=str(fake)):
                try:
                    await client.start()
                    result = await client.request('test/echo', {'text': '$(must remain text)'})
                    self.assertEqual(result['echo']['text'], '$(must remain text)')
                    with self.assertRaisesRegex(RuntimeError, 'expected failure'):
                        await client.request('test/error', {})
                finally:
                    await client.close()

    async def test_feishu_api_error_is_not_reported_as_success(self):
        def handler(request):
            if 'tenant_access_token' in request.url.path:
                return b.httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
            return b.httpx.Response(200, json={'code': 99991672, 'msg': 'missing permission'})
        client = b.Feishu(config())
        await client.client.aclose()
        client.client = b.httpx.AsyncClient(base_url='https://open.feishu.cn', transport=b.httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(RuntimeError, '99991672'):
                await client.send('chat-1', 'hello')
        finally:
            await client.client.aclose()


if __name__ == '__main__':
    os.umask(0o077)
    unittest.main(verbosity=2)

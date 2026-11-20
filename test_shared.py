import asyncio
from contextlib import suppress
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from test_bridge import b, config

spec = importlib.util.spec_from_file_location('shared_launcher', Path(__file__).with_name('shared_backend.py'))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class SharedBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = dict(config(), shared_socket='/tmp/example-shared.sock')
        self.bridge = b.Bridge(cfg, Path(self.tmp.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply = AsyncMock()
        self.bridge.codex = AsyncMock()
        self.bridge.state.bind('owner')
        self.bridge.state.select_thread('chat-1', 'thread-1', self.tmp.name)

    async def submit(self, text, chat='chat-1'):
        msg = dict(message_id=str(time.time_ns()), chat_id=chat, user_id='owner', chat_type='p2p',
                   text=text, created=time.time())
        self.bridge.state.receive(msg)
        await self.bridge.handle(msg)
        return msg

    async def external_start(self):
        await self.bridge.on_codex({'method': 'turn/started', 'params': {'threadId': 'thread-1',
            'turn': {'id': 'external-turn', 'status': 'inProgress', 'startedAt': time.time()}}})

    async def test_external_progress_result_and_duplicate_completion(self):
        await self.external_start()
        await self.bridge.on_codex({'method': 'item/completed', 'params': {'threadId': 'thread-1',
            'turnId': 'external-turn', 'item': {'id': 'answer', 'type': 'agentMessage',
                                             'phase': 'final_answer', 'text': '**共享答案**'}}})
        event = {'method': 'turn/completed', 'params': {'threadId': 'thread-1',
            'turn': {'id': 'external-turn', 'status': 'completed', 'items': []}}}
        await self.bridge.on_codex(event)
        await self.bridge.on_codex(event)
        replies = [call.args[1] for call in self.bridge.reply.await_args_list]
        self.assertEqual(sum('**共享答案**' in text for text in replies), 1)
        self.assertNotIn('thread-1', self.bridge.observed)

    async def test_external_turn_can_be_stopped_and_steered_from_feishu(self):
        await self.external_start()
        await self.submit('/steer 只检查这一个文件')
        self.bridge.codex.request.assert_awaited_with('turn/steer', {'threadId': 'thread-1',
            'expectedTurnId': 'external-turn', 'input': [{'type': 'text', 'text': '只检查这一个文件'}]})
        await self.submit('/stop')
        self.bridge.codex.request.assert_awaited_with('turn/interrupt', {'threadId': 'thread-1',
                                                                     'turnId': 'external-turn'})

    async def test_external_approval_requires_explicit_owner_and_other_client_can_resolve(self):
        await self.external_start()
        request = {'id': 9, 'method': 'item/commandExecution/requestApproval', 'params': {
            'threadId': 'thread-1', 'turnId': 'external-turn', 'command': 'echo example', 'cwd': '/tmp'}}
        await self.bridge.on_codex(request)
        await self.bridge.on_codex(request)
        self.assertEqual(len(self.bridge.approvals), 1)
        self.bridge.codex.respond.assert_not_called()
        code = next(iter(self.bridge.approvals))
        await self.bridge.on_codex({'method': 'serverRequest/resolved', 'params': {
            'threadId': 'thread-1', 'requestId': 9}})
        await self.submit('/approve ' + code)
        self.bridge.codex.respond.assert_not_called()

    async def test_unselected_threads_and_frontend_tools_are_left_to_their_client(self):
        await self.bridge.on_codex({'id': 5, 'method': 'item/commandExecution/requestApproval',
            'params': {'threadId': 'another-thread', 'command': 'echo private'}})
        await self.external_start()
        await self.bridge.on_codex({'id': 6, 'method': 'item/tool/call', 'params': {
            'threadId': 'thread-1', 'turnId': 'external-turn', 'tool': 'vscode-only-tool'}})
        self.bridge.codex.respond.assert_not_called()
        self.assertFalse(self.bridge.approvals)

    async def test_use_can_attach_to_running_thread_and_new_is_blocked(self):
        thread = {'id': 'thread-2', 'cwd': self.tmp.name, 'name': 'Existing running session',
                  'model': 'unchanged-model', 'modelProvider': 'openai', 'status': {'type': 'active'}}
        self.bridge.codex.request.side_effect = [
            {'thread': thread}, {'thread': thread}, {'data': [{'id': 'running-turn', 'status': 'inProgress'}]}]
        await self.submit('/use thread-2')
        self.assertEqual(self.bridge.state.thread('chat-1'), 'thread-2')
        self.assertEqual(self.bridge.observed['thread-2']['turn_id'], 'running-turn')
        await self.submit('/new')
        self.assertEqual(self.bridge.state.thread('chat-1'), 'thread-2')

    async def test_shared_worker_waits_for_external_turn_before_starting(self):
        await self.external_start()
        self.bridge.loaded_threads.add('thread-1')
        running = True
        async def request(method, params, **kwargs):
            nonlocal running
            if method == 'thread/read':
                return {'thread': {'id': 'thread-1', 'status': {'type': 'active' if running else 'idle'}}}
            if method == 'thread/turns/list':
                return {'data': [{'id': 'external-turn', 'status': 'inProgress'}]}
            if method == 'turn/start':
                self.assertFalse(running)
                self.bridge.active['final'] = 'own-result'
                self.bridge.active['done'].set_result({'id': 'own-turn', 'status': 'completed'})
                return {'turn': {'id': 'own-turn'}}
            raise AssertionError(method)
        self.bridge.codex.request.side_effect = request
        await self.submit('下一条任务')
        worker = asyncio.create_task(self.bridge.worker())
        try:
            for _ in range(100):
                if any('共享会话仍在执行' in call.args[1] for call in self.bridge.reply.await_args_list):
                    break
                await asyncio.sleep(0.005)
            self.assertFalse(any(call.args[0] == 'turn/start' for call in self.bridge.codex.request.await_args_list))
            running = False
            await self.bridge.on_codex({'method': 'turn/completed', 'params': {'threadId': 'thread-1',
                'turn': {'id': 'external-turn', 'status': 'completed', 'items': []}}})
            await asyncio.wait_for(self.bridge.jobs.join(), 2)
            self.assertEqual(self.bridge.state.rows('completed')[0]['result'], 'own-result')
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    async def test_completion_for_another_turn_does_not_finish_own_job(self):
        done = asyncio.get_running_loop().create_future()
        self.bridge.active = {'chat_id': 'chat-1', 'thread_id': 'thread-1', 'turn_id': 'own-turn',
                              'done': done, 'waiting_external': False}
        await self.bridge.on_codex({'method': 'turn/completed', 'params': {'threadId': 'thread-1',
            'turn': {'id': 'older-turn', 'status': 'completed', 'items': []}}})
        self.assertFalse(done.done())
        done.cancel()

    async def test_proxy_connection_does_not_disable_global_notifications(self):
        process = AsyncMock()
        process.returncode = None
        client = b.Codex(AsyncMock(), '/tmp/shared-test.sock')
        client.read = AsyncMock()
        client.request = AsyncMock(return_value={})
        client.write = AsyncMock()
        with patch.object(b.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)) as spawn:
            await client.start()
            arguments = spawn.await_args.args
            self.assertEqual(arguments, (b.sys.executable, str(b.ROOT / 'shared_backend.py'),
                                        '--proxy', '/tmp/shared-test.sock'))
            self.assertNotIn('notify=[]', arguments)
        await client.reader


class LauncherTests(unittest.TestCase):
    def test_only_stdio_server_start_is_redirected(self):
        yes = [['app-server'], ['-c', 'features.code_mode_host=true', 'app-server', '--analytics-default-enabled'],
               ['app-server', '--listen', 'stdio://']]
        no = [['--version'], ['exec', 'app-server'], ['app-server', 'proxy', '--sock', '/tmp/a'],
              ['app-server', 'daemon', 'version'], ['app-server', '--listen', 'unix:///tmp/a'],
              ['app-server', '--help']]
        for args in yes:
            self.assertTrue(launcher.is_stdio_app_server(args), args)
        for args in no:
            self.assertFalse(launcher.is_stdio_app_server(args), args)


if __name__ == '__main__':
    os.umask(0o077)
    unittest.main(verbosity=2)

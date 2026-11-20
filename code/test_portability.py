import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import codex_locator
from global_questions import GlobalQuestions
from question_channel import QuestionServer
from test_bridge import b, config


class LocatorTests(unittest.TestCase):
    def test_explicit_executable_is_used_and_invalid_path_fails_closed(self):
        self.assertEqual(codex_locator.find_codex(sys.executable), Path(sys.executable).resolve())
        with self.assertRaisesRegex(RuntimeError, 'missing or not executable'):
            codex_locator.find_codex('/not/a/codex/executable')


class InstallationIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_and_codex_home_are_passed_to_shared_proxy(self):
        process = AsyncMock()
        process.returncode = None
        client = b.Codex(AsyncMock(), '/private/shared.sock', runtime_dir='/private/install',
                         codex_home='/private/codex-home')
        client.read = AsyncMock()
        client.request = AsyncMock(return_value={})
        client.write = AsyncMock()
        with patch.object(b.asyncio, 'create_subprocess_exec', new=AsyncMock(return_value=process)) as spawn:
            await client.start()
            env = spawn.await_args.kwargs['env']
            self.assertEqual(env['CODEX_SHARED_ROOT'], '/private/install')
            self.assertEqual(env['CODEX_HOME'], '/private/codex-home')
        await client.reader

    async def test_question_channel_and_global_scan_use_the_selected_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / 'runtime'
            install.mkdir(mode=0o700)
            home = root / 'codex-home'
            bridge = b.Bridge(dict(config(), codex_home=str(home)), install)
            try:
                monitor = GlobalQuestions(bridge)
                self.assertEqual(monitor.sessions, home / 'sessions')
                self.assertEqual(monitor.ipc.path, home / 'ipc' / 'ipc.sock')
                server = QuestionServer(bridge, install / 'question.sock')
                await server.start()
                try:
                    self.assertTrue(server.path.is_socket())
                    self.assertEqual(os.stat(server.path).st_mode & 0o777, 0o600)
                finally:
                    await server.close()
            finally:
                bridge.state.db.close()
                await bridge.feishu.client.aclose()


if __name__ == '__main__':
    unittest.main()

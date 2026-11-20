import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from test_bridge import b, config


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = b.Bridge(dict(config(), shared_socket='/tmp/shared.sock'), Path(self.tmp.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.state.bind('owner')
        self.bridge.reply = AsyncMock()
        self.intent = Path(self.tmp.name) / 'pending-use.json'
        self.intent.write_text(json.dumps({'user_id': 'owner', 'chat_id': 'chat',
            'thread_id': 'desired-thread', 'expires': time.time() + 300}))

    async def test_locked_target_waits_then_switches_once_and_saves_receipt(self):
        self.bridge.use_session = AsyncMock(side_effect=RuntimeError('目标会话仍被另一个 Codex 实例占用。'))
        await self.bridge.recover_pending_use()
        self.assertTrue(self.intent.exists())
        self.bridge.reply.assert_not_called()
        async def switch(chat, thread):
            self.bridge.state.select_thread(chat, thread, '/tmp')
        self.bridge.use_session = AsyncMock(side_effect=switch)
        await self.bridge.recover_pending_use()
        await self.bridge.recover_pending_use()
        self.bridge.use_session.assert_awaited_once_with('chat', 'desired-thread')
        self.assertFalse(self.intent.exists())
        self.assertEqual(json.loads((Path(self.tmp.name) / 'last-auto-use.json').read_text())['status'], 'completed')

    async def test_expired_or_unauthorized_intent_never_changes_session(self):
        self.bridge.use_session = AsyncMock()
        self.intent.write_text(json.dumps({'user_id': 'stranger', 'expires': time.time() + 300}))
        await self.bridge.recover_pending_use()
        self.bridge.use_session.assert_not_called()
        self.assertFalse(self.intent.exists())

    async def test_active_job_delays_recovery_and_real_error_is_reported_once(self):
        self.bridge.use_session = AsyncMock(side_effect=RuntimeError('目标项目目录不可用'))
        self.bridge.active = {'thread_id': 'busy'}
        await self.bridge.recover_pending_use()
        self.bridge.use_session.assert_not_called()
        self.bridge.active = None
        await self.bridge.recover_pending_use()
        await self.bridge.recover_pending_use()
        self.bridge.use_session.assert_awaited_once()
        self.bridge.reply.assert_awaited_once()
        self.assertFalse(self.intent.exists())


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from test_bridge import b, config


class CompactCardTests(unittest.TestCase):
    def test_status_is_short_and_single_card_retains_complete_phone_text(self):
        text = '## 本轮详细结果\n\n' + '已检查代码并完成当前验证。' * 100 + '\n【正文结束】'
        cards = b.message_cards(text, dict(config(), compact_notifications=True), notice='已完成')
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['header']['title']['content'], '已完成')
        self.assertEqual(cards[0]['config']['summary']['content'], '已完成')
        self.assertEqual(cards[0]['body']['elements'][0]['content'], text)

    def test_progress_status_is_explicit_and_not_guessed_from_body(self):
        card = b.message_cards('还没完成，但已修复一处错误。',
            dict(config(), compact_notifications=True), notice='执行中')[0]
        self.assertEqual(card['config']['summary']['content'], '执行中')
        self.assertNotIn('…', card['header']['title']['content'])

    def test_generic_message_uses_short_neutral_notice(self):
        card = b.message_cards('这里是一段很长的通知正文。' * 20,
            dict(config(), compact_notifications=True))[0]
        self.assertEqual(card['header']['title']['content'], '新消息')

    def test_question_keeps_all_text_options_and_original_callback(self):
        pending = {'params': {'questions': [{'id': 'q1', 'question': '请回答完整问题。' * 50,
            'options': [{'label': '第一项'}, {'label': '第二项'}]}]}}
        cards = b.question_cards('request-code', pending, dict(config(), compact_notifications=True))
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card['header']['title']['content'], '待回答')
        self.assertEqual(card['config']['summary']['content'], '待回答')
        self.assertIn('请回答完整问题。' * 50, card['body']['elements'][0]['content'])
        self.assertEqual(card['body']['elements'][-1]['behaviors'][0]['value'], {
            'action': 'codex_answer', 'code': 'request-code', 'question': 'q1', 'option': 2})


class CompactBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = b.Bridge(dict(config(), shared_socket='/tmp/shared.sock', compact_notifications=True),
                               Path(self.tmp.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply = AsyncMock(return_value=True)
        self.bridge.codex = AsyncMock()
        self.bridge.state.bind('owner')
        self.bridge.state.select_thread('chat', 'thread', '/tmp')
        self.bridge.loaded_threads.add('thread')

    async def start_turn(self):
        await self.bridge.on_codex({'method': 'turn/started', 'params': {
            'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'inProgress'}}})

    async def finish_turn(self, status='completed'):
        await self.bridge.on_codex({'method': 'turn/completed', 'params': {
            'threadId': 'thread', 'turn': {'id': 'turn', 'status': status,
                'items': [{'type': 'agentMessage', 'phase': 'final_answer', 'text': '实际完整结果'}]}}})

    async def test_mirrored_progress_and_completion_have_accurate_status(self):
        await self.start_turn()
        # The installed bridge intentionally does not send start notifications.
        self.bridge.reply.assert_not_awaited()
        self.bridge.observed['thread']['last_progress'] = 0
        await self.bridge.on_codex({'method': 'item/completed', 'params': {
            'threadId': 'thread', 'item': {'type': 'agentMessage', 'phase': 'commentary', 'text': '还没完成所有任务'}}})
        self.assertEqual(self.bridge.reply.await_args.kwargs['notice'], '执行中')
        await self.finish_turn()
        self.assertEqual(self.bridge.reply.await_args.kwargs['notice'], '已完成')
        self.assertIn('实际完整结果', self.bridge.reply.await_args.args[1])

    async def test_muted_chat_only_sends_questions_and_approvals(self):
        self.bridge.reply = b.Bridge.reply.__get__(self.bridge)
        self.bridge.feishu.send = AsyncMock(return_value=['sent'])
        message = {'message_id': 'mute', 'chat_id': 'chat', 'user_id': 'owner',
                   'chat_type': 'p2p', 'text': '/mute'}
        await self.bridge.handle(message)
        self.assertTrue(self.bridge.state.chat_muted('chat'))
        self.assertFalse(self.bridge.state.chat_muted('other-chat'))
        self.bridge.feishu.send.assert_not_awaited()
        await self.start_turn()
        self.bridge.observed['thread']['last_progress'] = 0
        progress = {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
            'item': {'type': 'agentMessage', 'phase': 'commentary', 'text': '后台正在检查'}}}
        await self.bridge.on_codex(progress)
        self.bridge.feishu.send.assert_not_awaited()
        self.assertEqual(self.bridge.observed['thread']['progress'], '后台正在检查')
        await self.bridge.handle(dict(message, message_id='status', text='/status'))
        self.bridge.feishu.send.assert_not_awaited()

        await self.bridge.on_codex({'id': 1, 'method': 'item/commandExecution/requestApproval', 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'command': 'echo example'}})
        self.assertEqual(self.bridge.feishu.send.await_args.kwargs, {'notice': '待审批', 'allow_muted': True})
        self.bridge.feishu.send_cards = AsyncMock()
        await self.bridge.on_codex({'id': 2, 'method': 'item/tool/requestUserInput', 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'questions': [{'id': 'q', 'question': '继续吗？'}]}})
        self.bridge.feishu.send_cards.assert_awaited_once()
        self.assertTrue(self.bridge.feishu.send_cards.await_args.kwargs['allow_muted'])
        self.bridge.feishu.send_cards.side_effect = RuntimeError('card unavailable')
        await self.bridge.on_codex({'id': 3, 'method': 'item/tool/requestUserInput', 'params': {
            'threadId': 'thread', 'turnId': 'turn', 'questions': [{'id': 'q', 'question': '需要答复'}]}})
        self.assertIn('需要答复', self.bridge.feishu.send.await_args.args[1])
        self.assertTrue(self.bridge.feishu.send.await_args.kwargs['allow_muted'])
        self.bridge.feishu.send.reset_mock()
        await self.finish_turn()
        self.bridge.feishu.send.assert_not_awaited()

        await self.bridge.handle(dict(message, message_id='unmute', text='/unmute'))
        self.assertFalse(self.bridge.state.chat_muted('chat'))
        await self.start_turn()
        self.bridge.observed['thread']['last_progress'] = 0
        await self.bridge.on_codex(progress)
        self.assertEqual(self.bridge.feishu.send.await_args.kwargs['notice'], '执行中')

    async def test_muting_shared_wait_does_not_bypass_the_wait(self):
        self.bridge.state.set_chat_muted('chat', True)
        self.bridge.codex.request.side_effect = [
            {'thread': {'status': {'type': 'active'}}}, {'thread': {'status': {'type': 'idle'}}}]

        async def refreshed(*args):
            self.bridge.runtime_changed.set()

        self.bridge.refresh_runtime = AsyncMock(side_effect=refreshed)
        await self.bridge.wait_shared_idle('thread', 'chat')
        self.assertEqual(self.bridge.codex.request.await_count, 2)
        self.bridge.reply.assert_not_awaited()

    async def test_native_active_goal_is_not_reported_complete_when_one_turn_finishes(self):
        self.bridge.goals.mark_managed('thread', 'chat')
        self.bridge.goals.cache['thread'] = {'status': 'active'}
        await self.start_turn()
        await self.finish_turn()
        self.assertEqual(self.bridge.reply.await_args.kwargs['notice'], '执行中')
        await self.start_turn()
        self.bridge.goals.cache['thread'] = {'status': 'complete'}
        await self.finish_turn()
        self.assertEqual(self.bridge.reply.await_args.kwargs['notice'], '已完成')

    async def test_interrupted_and_failed_tasks_do_not_show_done(self):
        for status, expected in [('interrupted', '已中止'), ('failed', '执行失败')]:
            await self.start_turn()
            await self.finish_turn(status)
            self.assertEqual(self.bridge.reply.await_args.kwargs['notice'], expected)

    def test_all_native_goal_states_have_short_accurate_notice(self):
        for status, expected in [('active', '执行中'), ('paused', '已暂停'), ('complete', '已完成'),
                ('blocked', '待处理'), ('usageLimited', '用量受限'), ('budgetLimited', '预算受限')]:
            self.bridge.goals.cache['thread'] = {'status': status}
            self.assertEqual(self.bridge.goals.notification_status('thread'), expected)


if __name__ == '__main__':
    unittest.main()

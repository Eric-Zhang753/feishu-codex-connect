import asyncio
from contextlib import suppress
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from test_bridge import b, config


def catalog():
    return [
        {'id': 'first', 'model': 'model-a', 'displayName': 'Model A', 'isDefault': True,
         'defaultReasoningEffort': 'medium', 'supportedReasoningEfforts': [
             {'reasoningEffort': 'low'}, {'reasoningEffort': 'medium'}, {'reasoningEffort': 'high'}]},
        {'id': 'second', 'model': 'model-b', 'displayName': 'Model B', 'isDefault': False,
         'defaultReasoningEffort': 'low', 'supportedReasoningEfforts': [
             {'reasoningEffort': 'low'}, {'reasoningEffort': 'medium'}]},
    ]


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge = b.Bridge(dict(config(), shared_socket='/tmp/shared.sock'), Path(self.tmp.name))
        self.addCleanup(self.bridge.state.db.close)
        self.addAsyncCleanup(self.bridge.feishu.client.aclose)
        self.bridge.reply = AsyncMock()
        self.bridge.codex = AsyncMock()
        self.bridge.state.bind('owner')
        self.models = catalog()
        self.thread = {'id': 'private-thread-identifier', 'name': 'Research task', 'cwd': self.tmp.name,
                       'model': 'model-a', 'modelProvider': 'openai', 'reasoningEffort': 'high',
                       'status': {'type': 'idle'}, 'updatedAt': int(time.time())}
        self.bridge.codex.request.side_effect = self.request

    async def request(self, method, params, **kwargs):
        if method == 'model/list':
            return {'data': self.models, 'nextCursor': None}
        if method == 'config/read':
            return {'config': {'model': 'model-a', 'model_reasoning_effort': 'high'}}
        if method in ('thread/read', 'thread/resume'):
            return {'thread': dict(self.thread)}
        if method == 'thread/list':
            return {'data': [dict(self.thread)], 'nextCursor': None}
        if method == 'thread/settings/update':
            self.thread['model'] = params['model']
            if 'effort' in params:
                self.thread['reasoningEffort'] = params['effort']
            return {}
        raise AssertionError(method)

    def select(self):
        self.bridge.state.select_thread('chat', self.thread['id'], self.tmp.name, 'model-a', 'openai', 'high')
        self.bridge.loaded_threads.add(self.thread['id'])

    async def submit(self, text, user='owner'):
        message = dict(message_id=str(time.time_ns()), chat_id='chat', user_id=user,
                       chat_type='p2p', text=text, created=time.time())
        self.bridge.state.receive(message)
        await self.bridge.handle(message)

    def mutations(self):
        return [call.args[1] for call in self.bridge.codex.request.await_args_list
                if call.args[0] == 'thread/settings/update']

    async def test_ids_are_hidden_until_explicitly_requested(self):
        self.select()
        for command in ('/sessions', '/current', '/use 1'):
            await self.submit(command)
            text = self.bridge.reply.await_args.args[1]
            self.assertNotIn(self.thread['id'], text)
            self.assertIn('Research task', text)
        await self.submit('/current id')
        self.assertIn(self.thread['id'], self.bridge.reply.await_args.args[1])

    async def test_switch_model_preserves_supported_effort_and_changes_no_permissions(self):
        self.select()
        await self.submit('/model model-a')
        self.assertEqual(self.mutations(), [{'threadId': self.thread['id'], 'model': 'model-a', 'effort': 'high'}])
        self.assertNotIn(self.thread['id'], self.bridge.reply.await_args.args[1])
        self.assertIn('后续轮次生效', self.bridge.reply.await_args.args[1])

    async def test_new_model_uses_its_default_when_old_effort_is_unsupported(self):
        self.select()
        await self.submit('/model model-b')
        self.assertEqual(self.mutations()[0]['effort'], 'low')
        self.assertEqual(self.bridge.state.context('chat')['reasoning_effort'], 'low')

    async def test_model_number_uses_the_displayed_snapshot(self):
        self.select()
        await self.submit('/models')
        self.models.reverse()
        await self.submit('/model 2 medium')
        self.assertEqual(self.mutations(), [{'threadId': self.thread['id'], 'model': 'model-b', 'effort': 'medium'}])

    async def test_expired_model_number_does_not_modify_settings(self):
        self.select()
        self.bridge.state.set_setting('model-list:chat', json.dumps({
            'created': time.time() - 1900, 'models': ['model-a']}))
        await self.submit('/model 1')
        self.assertFalse(self.mutations())
        self.bridge.codex.request.assert_not_called()

    async def test_unknown_model_or_unsupported_effort_is_rejected(self):
        self.select()
        for command in ('/model made-up', '/model model-b ultra', '/effort ultra', '/effort 99'):
            await self.submit(command)
        self.assertFalse(self.mutations())
        self.assertEqual(self.bridge.state.context('chat')['model'], 'model-a')

    async def test_effort_changes_use_authoritative_model_and_accept_alias_or_default(self):
        self.select()
        self.thread.update(model='model-b', reasoningEffort='low')
        await self.submit('/effort 中')
        self.assertEqual(self.mutations()[-1]['model'], 'model-b')
        self.assertEqual(self.mutations()[-1]['effort'], 'medium')
        await self.submit('/effort default')
        self.assertEqual(self.mutations()[-1]['effort'], 'low')

    async def test_reading_effort_does_not_mutate_or_start_a_turn(self):
        self.select()
        await self.submit('/effort')
        text = self.bridge.reply.await_args.args[1]
        self.assertIn('思考强度：high', text)
        self.assertIn('1. `low`', text)
        self.assertFalse(self.mutations())
        self.assertFalse(any(call.args[0] == 'turn/start' for call in self.bridge.codex.request.await_args_list))

    async def test_preferences_before_first_thread_survive_state_reopen(self):
        await self.submit('/model model-b medium')
        self.assertFalse(self.mutations())
        self.assertIsNone(self.bridge.state.thread('chat'))
        state = b.State(Path(self.tmp.name) / 'state.sqlite3')
        try:
            context = state.context('chat')
            self.assertEqual(context['model'], 'model-b')
            self.assertEqual(context['reasoning_effort'], 'medium')
        finally:
            state.db.close()

    async def test_new_thread_applies_preferences_and_completion_omits_id(self):
        await self.submit('/model model-b medium')
        original = self.request
        async def request(method, params, **kwargs):
            if method == 'thread/start':
                self.assertEqual(params['model'], 'model-b')
                self.assertEqual(params['config'], {'notify': [], 'model_reasoning_effort': 'medium'})
                return {'thread': dict(self.thread, model='model-b', reasoningEffort='medium')}
            if method == 'turn/start':
                self.assertNotIn('model', params)
                self.assertNotIn('effort', params)
                self.bridge.active['final'] = 'Completed result'
                self.bridge.active['done'].set_result({'id': 'turn', 'status': 'completed'})
                return {'turn': {'id': 'turn'}}
            return await original(method, params, **kwargs)
        self.bridge.codex.request.side_effect = request
        await self.submit('Do the task')
        worker = asyncio.create_task(self.bridge.worker())
        try:
            await asyncio.wait_for(self.bridge.jobs.join(), 3)
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        text = self.bridge.reply.await_args.args[1]
        self.assertIn('Completed result', text)
        self.assertNotIn(self.thread['id'], text)

    async def test_shared_settings_notifications_refresh_cached_preferences_while_idle(self):
        self.select()
        await self.bridge.on_codex({'method': 'thread/settings/updated', 'params': {
            'threadId': self.thread['id'], 'threadSettings': {'model': 'model-b', 'effort': 'medium'}}})
        context = self.bridge.state.context('chat')
        self.assertEqual(context['model'], 'model-b')
        self.assertEqual(context['reasoning_effort'], 'medium')
        self.bridge.reply.assert_not_called()

    async def test_future_settings_can_change_during_an_active_turn(self):
        self.select()
        self.thread['status'] = {'type': 'active'}
        await self.submit('/model model-b')
        self.assertEqual(len(self.mutations()), 1)
        self.assertFalse(any(call.args[0] in ('turn/interrupt', 'turn/start', 'turn/settings/update')
                             for call in self.bridge.codex.request.await_args_list))

    async def test_failed_update_does_not_claim_success_or_replace_cached_settings(self):
        self.select()
        original = self.request
        async def request(method, params, **kwargs):
            if method == 'thread/settings/update':
                raise RuntimeError('backend rejected setting')
            return await original(method, params, **kwargs)
        self.bridge.codex.request.side_effect = request
        with self.assertRaisesRegex(RuntimeError, 'backend rejected'):
            await self.submit('/model model-b')
        self.assertEqual(self.bridge.state.context('chat')['model'], 'model-a')
        self.bridge.reply.assert_not_called()

    async def test_unauthorized_user_cannot_access_model_controls(self):
        for command in ('/models', '/model model-b', '/effort high'):
            await self.submit(command, user='stranger')
        self.bridge.codex.request.assert_not_called()

    async def test_model_pagination_is_followed_and_hidden_models_are_omitted(self):
        self.bridge.codex.request.side_effect = [
            {'data': [dict(catalog()[0], hidden=True)], 'nextCursor': 'page-2'},
            {'data': [catalog()[1]], 'nextCursor': None}]
        result = await self.bridge.available_models()
        self.assertEqual([item['model'] for item in result], ['model-b'])
        self.assertEqual(self.bridge.codex.request.await_args.args[1]['cursor'], 'page-2')


if __name__ == '__main__':
    unittest.main(verbosity=2)

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import setup_feishu as setup


FULL_ADDONS = {
    'preset': False,
    'scopes': {'tenant': ['im:message.p2p_msg:readonly', 'im:message:send_as_bot', 'im:resource',
                          'application:app_slash_command:read', 'application:app_slash_command:write',
                          'im:message.reactions:write_only', 'im:message.group_at_msg:readonly',
                          'im:message.pins:write_only']},
    'events': {'items': {'tenant': ['im.message.receive_v1']}},
    'callbacks': {'items': ['card.action.trigger']},
}


class FeishuSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_config_saves_scanner_and_group_bot_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = project / 'runtime' / 'config.json'
            result = {'client_id': 'cli_test', 'client_secret': 'secret',
                      'user_info': {'open_id': 'ou_scanner'}}
            with (patch.object(setup, 'register', new=AsyncMock(return_value=result)) as register,
                  patch.object(setup, 'bot_info', new=AsyncMock(return_value={
                      'open_id': 'ou_bot', 'activate_status': 2})),
                  patch('builtins.print')):
                await setup.create_bot(path, project, group=True, monitor=True)

            config = setup.load_config(path)
            self.assertEqual(config['allowed_user_ids'], ['ou_scanner'])
            self.assertEqual(config['bot_open_id'], 'ou_bot')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            register.assert_awaited_once_with(FULL_ADDONS)

    async def test_registration_returning_bot_id_keeps_pairing_required(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = project / 'runtime' / 'config.json'
            result = {'client_id': 'cli_test', 'client_secret': 'secret',
                      'user_info': {'open_id': 'ou_bot'}}
            with (patch.object(setup, 'register', new=AsyncMock(return_value=result)) as register,
                  patch.object(setup, 'bot_info', new=AsyncMock(return_value={'open_id': 'ou_bot'})),
                  patch('builtins.print')):
                await setup.create_bot(path, project, group=False, monitor=False)
            config = setup.load_config(path)
            self.assertEqual(config['allowed_user_ids'], [])
            self.assertNotIn('bot_open_id', config)
            register.assert_awaited_once_with(FULL_ADDONS)

    async def test_update_requests_all_features_without_replacing_credentials_or_state(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = project / 'config.json'
            config = {'app_id': 'cli_current', 'app_secret': 'current', 'project_dir': directory,
                      'allowed_user_ids': ['ou_owner'], 'message_feedback_enabled': False}
            setup.atomic_json(path, config)
            state = project / 'state.sqlite3'
            state.write_bytes(b'current state')
            result = {'client_id': 'cli_current', 'client_secret': 'different'}
            with (patch.object(setup, 'register', new=AsyncMock(return_value=result)) as register,
                  patch('builtins.print')):
                await setup.update_permissions(path, group=False, monitor=False)

            register.assert_awaited_once_with(FULL_ADDONS, app_id='cli_current')
            self.assertEqual(json.loads(path.read_bytes()), config)
            self.assertEqual(state.read_bytes(), b'current state')

    async def test_configured_permissions_preserve_selective_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            path = project / 'config.json'
            setup.atomic_json(path, {'app_id': 'cli_current', 'app_secret': 'current', 'project_dir': directory,
                                     'message_feedback_enabled': False})
            setup.atomic_json(project / 'server-monitor.json', {'enabled': True})
            with (patch.object(setup, 'register', new=AsyncMock(return_value={'client_id': 'cli_current'})) as register,
                  patch('builtins.print')):
                await setup.update_permissions(path, group=False, monitor=False, configured_permissions=True)

            scopes = register.await_args.args[0]['scopes']['tenant']
            self.assertEqual(scopes, FULL_ADDONS['scopes']['tenant'][:5] + ['im:message.pins:write_only'])

    async def test_create_rejects_a_directory_with_existing_bridge_state_before_remote_call(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / 'state.sqlite3').touch()
            with patch.object(setup, 'register', new=AsyncMock()) as register:
                with self.assertRaises(setup.SetupError):
                    await setup.create_bot(project / 'other.json', project, group=False, monitor=False)
            register.assert_not_awaited()

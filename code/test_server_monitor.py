import copy
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import server_monitor as m


def metrics():
    return {'sampled_at': '2026-11-09T12:20:00+08:00', 'cpu_percent': 25.0,
            'cpu_count': 32, 'memory_used_gib': 20.0, 'memory_total_gib': 64.0,
            'gpus': m.parse_gpus('0, GPU A, 95, 6400, 49140, 64\n'
                                '1, GPU B, 0, 0, 49140, 30\n'), 'gpu_error': None,
            'network': {'received_bytes_s': 334.2 * 1024, 'sent_bytes_s': 339.5 * 1024},
            'disk': {'path': '/', 'used_bytes': 885 * 1024**3,
                     'total_bytes': 885 * 1024**3 / 0.586, 'percent': 58.6}}


class MetricsTests(unittest.TestCase):
    def test_gpu_na_is_unavailable_not_zero_and_bad_output_is_rejected(self):
        gpu = m.parse_gpus('1, NVIDIA, [N/A], [N/A], 49140, N/A\n')[0]
        self.assertIsNone(gpu['utilization'])
        self.assertIsNone(gpu['used_mib'])
        for value in ['0, broken\n', 'not a GPU id, name, 95, 6400, 49140, 64\n']:
            with self.assertRaises(ValueError):
                m.parse_gpus(value)

    def test_card_distinguishes_compute_and_memory_and_has_data_timestamp(self):
        card = m.card(metrics())
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn('GPU0 95%', text)
        self.assertIn('GPU1 0%', text)
        self.assertIn('6.25/47.99 GiB', text)
        self.assertIn('6.25/95.98 GiB', text)
        self.assertIn('64°C', text)
        self.assertIn('334.2 KiB/s', text)
        self.assertIn('339.5 KiB/s', text)
        self.assertIn('58.6%', text)
        self.assertIn('885 GiB/1.47 TiB', text)
        self.assertIn('11-09 12:20:00', text)
        self.assertTrue(card['config']['update_multi'])

    def test_missing_gpu_is_not_presented_as_idle(self):
        data = metrics()
        data.update(gpus=[], gpu_error='GPU 指标暂不可用')
        card = m.card(data)
        self.assertIn('GPU 不可用', card['header']['title']['content'])
        self.assertNotIn('GPU0 0%', json.dumps(card))


class SamplingTests(unittest.TestCase):
    def test_network_uses_default_uplink_without_duplicate_tunnel_or_bridge_traffic(self):
        counters = {name: SimpleNamespace(bytes_recv=received, bytes_sent=sent)
                    for name, received, sent in [('ens34', 100, 200), ('lo', 1000, 1000),
                                                ('tailscale0', 100, 200), ('docker0', 300, 300)]}
        route = 'Iface Destination Gateway Flags\nens34 00000000 01020304 0003\n'
        with patch.object(m.psutil, 'net_io_counters', return_value=counters), \
                patch.object(m.Path, 'read_text', return_value=route):
            snapshot = m.network_snapshot()
        self.assertEqual(snapshot['interfaces'], ['ens34'])
        self.assertEqual((snapshot['received'], snapshot['sent']), (100, 200))

    def test_rates_use_actual_elapsed_time_and_reject_counter_resets_or_interface_changes(self):
        before = {'interfaces': ['ens34'], 'time': 10, 'received': 1000, 'sent': 3000}
        after = {'interfaces': ['ens34'], 'time': 12, 'received': 3048, 'sent': 7096}
        rates = m.network_rates(before, after)
        self.assertEqual(rates['received_bytes_s'], 1024)
        self.assertEqual(rates['sent_bytes_s'], 2048)
        for changes in ({'interfaces': ['eth0']}, {'received': 0}, {'time': 10}):
            self.assertIsNone(m.network_rates(before, {**after, **changes}))
        self.assertIsNone(m.network_rates(None, after))

    def test_disk_percentage_matches_used_over_total_rather_than_available_space(self):
        usage = SimpleNamespace(used=586, total=1000, percent=61.3)
        with patch.object(m.psutil, 'disk_usage', return_value=usage):
            self.assertAlmostEqual(m.disk_metrics()['percent'], 58.6)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / 'state.json'
        self.api = AsyncMock()
        self.api.send.return_value = 'monitor-message-1'
        self.settings = {'enabled': True, 'chat_id': 'owner-chat'}
        self.monitor = m.Monitor(self.api, self.settings, self.state_path)

    async def test_repeated_refresh_and_process_restart_reuse_same_message(self):
        await self.monitor.tick(metrics())
        await self.monitor.tick(metrics())
        restored = m.Monitor(self.api, self.settings, self.state_path)
        await restored.tick(metrics())
        self.api.send.assert_awaited_once()
        self.assertEqual(self.api.update.await_count, 2)
        self.assertTrue(all(call.args[0] == 'monitor-message-1'
                            for call in self.api.update.await_args_list))
        self.api.pin.assert_awaited_once()
        self.assertEqual(restored.state['update_count'], 3)
        self.assertEqual(self.state_path.stat().st_mode & 0o077, 0)

    async def test_permission_failure_only_requires_manual_pin_and_never_resends(self):
        self.api.pin.side_effect = m.ApiError(99991672)
        await self.monitor.tick(metrics())
        await self.monitor.tick(metrics())
        self.api.send.assert_awaited_once()
        self.api.pin.assert_awaited_once()
        self.assertEqual(self.monitor.state['pin_state'], 'manual_required')
        self.assertEqual(self.monitor.state['pin_error_code'], 99991672)
        self.assertIn('选择“Pin”', json.dumps(self.api.update.call_args.args[1], ensure_ascii=False))

    async def test_temporary_edit_failure_does_not_create_another_message(self):
        await self.monitor.tick(metrics())
        self.api.update.side_effect = m.ApiError(99991400)
        with self.assertRaises(m.ApiError):
            await self.monitor.tick(metrics())
        self.api.send.assert_awaited_once()
        self.assertEqual(self.monitor.state['message_id'], 'monitor-message-1')

    async def test_user_deleted_message_pauses_monitor_without_recreating(self):
        await self.monitor.tick(metrics())
        self.api.update.side_effect = m.ApiError(230110)
        await self.monitor.tick(metrics())
        await self.monitor.tick(metrics())
        self.api.send.assert_awaited_once()
        self.assertTrue(self.monitor.state['paused'])
        self.assertTrue(self.monitor.stop.is_set())

    async def test_old_card_is_retired_and_replaced_before_feishu_edit_deadline(self):
        await self.monitor.tick(metrics())
        self.monitor.state['created_at'] = time.time() - m.RENEW_AFTER - 60
        self.api.send.return_value = 'monitor-message-2'
        await self.monitor.tick(metrics())
        self.assertEqual(self.api.send.await_count, 2)
        self.assertEqual(self.monitor.state['message_id'], 'monitor-message-2')
        self.assertEqual(self.api.pin.await_count, 2)
        retired = self.api.update.call_args.args[1]
        self.assertIn('已续期', retired['header']['title']['content'])
        self.assertNotIn('95%', json.dumps(retired))

    async def test_server_expiry_response_renews_even_when_local_age_is_young(self):
        await self.monitor.tick(metrics())
        self.api.update.side_effect = m.ApiError(230031)
        self.api.send.return_value = 'monitor-message-2'
        await self.monitor.tick(metrics())
        self.assertEqual(self.api.send.await_count, 2)
        self.assertEqual(self.monitor.state['message_id'], 'monitor-message-2')

    async def test_uncertain_send_outcome_reuses_idempotency_key_after_restart(self):
        self.api.send.side_effect = RuntimeError('network timeout after accepting send')
        with self.assertRaises(RuntimeError):
            await self.monitor.tick(metrics())
        first_key = self.api.send.call_args.args[2]
        self.api.send.side_effect = None
        restored = m.Monitor(self.api, self.settings, self.state_path)
        await restored.tick(metrics())
        self.assertEqual(self.api.send.call_args.args[2], first_key)
        self.assertNotIn('pending_uuid', restored.state)


if __name__ == '__main__':
    unittest.main()

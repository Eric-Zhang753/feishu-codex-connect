#!/usr/bin/env python3
"""Update one Feishu server-metrics card, independently of Codex sessions."""
import argparse
import asyncio
from contextlib import suppress
import csv
from datetime import datetime
import fcntl
import io
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import time
from urllib.parse import quote
import uuid
from zoneinfo import ZoneInfo

import httpx
import psutil

LOG = logging.getLogger('codex-server-monitor')
ROOT = Path(__file__).resolve().parent
REFRESH_SECONDS = 30
RENEW_AFTER = 13 * 86400  # Feishu stops accepting message edits after 14 days.
AUTH_CODES = {99991661, 99991663, 99991668}
DELETED_CODES = {230011, 230110}


def atomic_json(path, data):
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_private(path):
    if path.stat().st_mode & 0o077:
        raise ValueError('Monitor files must be owner-only.')
    return json.loads(path.read_text())


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def parse_gpus(output):
    devices = []
    for row in csv.reader(io.StringIO(output)):
        if not row:
            continue
        if len(row) != 6 or number(row[0]) is None:
            raise ValueError('Invalid GPU metrics response.')
        index, name, utilization, used, total, temperature = (x.strip() for x in row)
        devices.append({'index': int(index), 'name': name,
                        'utilization': number(utilization), 'used_mib': number(used),
                        'total_mib': number(total), 'temperature': number(temperature)})
    return sorted(devices, key=lambda item: item['index'])


async def gpu_metrics():
    binary = shutil.which('nvidia-smi')
    if not binary:
        return [], '未检测到 nvidia-smi'
    process = None
    try:
        process = await asyncio.create_subprocess_exec(binary,
            '--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu',
            '--format=csv,noheader,nounits', stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        if process.returncode != 0:
            return [], 'GPU 指标暂不可用'
        return parse_gpus(output.decode('utf-8')), None
    except (OSError, ValueError, UnicodeError, asyncio.TimeoutError):
        return [], 'GPU 指标暂不可用'
    finally:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def network_snapshot():
    """Read the uplink only, excluding loopback and duplicated bridge/tunnel traffic."""
    try:
        counters = psutil.net_io_counters(pernic=True, nowrap=True)
        interfaces = set()
        try:
            for route in Path('/proc/net/route').read_text().splitlines()[1:]:
                fields = route.split()
                if len(fields) > 3 and fields[1] == '00000000' and int(fields[3], 16) & 1:
                    interfaces.add(fields[0])
        except (OSError, ValueError):
            pass
        if not interfaces:
            interfaces = {path.name for path in Path('/sys/class/net').iterdir()
                          if (path / 'device').exists()}
        interfaces = sorted(name for name in interfaces if name != 'lo' and name in counters)
        if not interfaces:
            return None
        return {'interfaces': interfaces, 'time': time.monotonic(),
                'received': sum(counters[name].bytes_recv for name in interfaces),
                'sent': sum(counters[name].bytes_sent for name in interfaces)}
    except (OSError, NotImplementedError):
        return None


def network_rates(before, after):
    if not before or not after or before['interfaces'] != after['interfaces']:
        return None
    elapsed = after['time'] - before['time']
    received, sent = after['received'] - before['received'], after['sent'] - before['sent']
    if elapsed <= 0 or received < 0 or sent < 0:
        return None
    return {'interfaces': after['interfaces'], 'received_bytes_s': received / elapsed,
            'sent_bytes_s': sent / elapsed, 'sample_seconds': elapsed}


def disk_metrics():
    try:
        usage = psutil.disk_usage('/')
        return {'path': '/', 'used_bytes': usage.used, 'total_bytes': usage.total,
                'percent': usage.used / usage.total * 100 if usage.total else None}
    except OSError:
        return None


async def sample():
    network_before = network_snapshot()
    cpu, (gpus, gpu_error) = await asyncio.gather(
        asyncio.to_thread(psutil.cpu_percent, interval=1), gpu_metrics())
    network = network_rates(network_before, network_snapshot())
    memory = psutil.virtual_memory()
    return {'sampled_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
            'cpu_percent': cpu, 'cpu_count': psutil.cpu_count(),
            'memory_used_gib': (memory.total - memory.available) / 1024 ** 3,
            'memory_total_gib': memory.total / 1024 ** 3,
            'gpus': gpus, 'gpu_error': gpu_error, 'network': network, 'disk': disk_metrics()}


def percent(value):
    return '不可用' if value is None else f'{value:.0f}%'


def throughput(value):
    if value >= 1024 ** 2:
        return f'{value / 1024 ** 2:.1f} MiB/s'
    if value >= 1024:
        return f'{value / 1024:.1f} KiB/s'
    return f'{value:.0f} B/s'


def disk_capacity(value):
    if value >= 1024 ** 4:
        return f'{value / 1024 ** 4:.2f} TiB'
    return f'{value / 1024 ** 3:.0f} GiB'


def card(metrics, pin_state=None, renewed=False):
    title_parts = ['GPU' + str(gpu['index']) + ' ' + percent(gpu['utilization'])
                   for gpu in metrics['gpus']]
    if metrics['gpu_error']:
        title_parts.append('GPU 不可用')
    title_parts.append('CPU ' + percent(metrics['cpu_percent']))
    title = ' · '.join(title_parts)
    lines = []
    for gpu in metrics['gpus']:
        used, total = gpu['used_mib'], gpu['total_mib']
        if used is not None and total:
            details = f'显存 {used / 1024:.2f}/{total / 1024:.2f} GiB'
        else:
            details = '显存暂不可用'
        if gpu['temperature'] is not None:
            details += f" · {gpu['temperature']:.0f}°C"
        lines.append(f"**GPU{gpu['index']}**　" + details)
    if metrics['gpu_error']:
        lines.append(metrics['gpu_error'])
    gpus = metrics['gpus']
    if gpus and not metrics['gpu_error'] and all(
            gpu['used_mib'] is not None and gpu['total_mib'] for gpu in gpus):
        used = sum(gpu['used_mib'] for gpu in gpus) / 1024
        total = sum(gpu['total_mib'] for gpu in gpus) / 1024
        lines.append(f'**显存合计**　{used:.2f}/{total:.2f} GiB')
    else:
        lines.append('**显存合计**　暂不可用')
    lines += [f"**CPU**　{metrics['cpu_percent']:.2f}% · {metrics['cpu_count']} 线程",
              f"**内存**　{metrics['memory_used_gib']:.2f}/{metrics['memory_total_gib']:.2f} GiB"]
    network = metrics.get('network')
    if network:
        lines.append('**网络**　↓' + throughput(network['received_bytes_s'])
                     + '　↑' + throughput(network['sent_bytes_s']))
    else:
        lines.append('**网络**　暂不可用')
    disk = metrics.get('disk')
    if disk:
        usage = f"{disk['percent']:.1f}%" if disk['percent'] is not None else '不可用'
        lines.append(f"**磁盘 {disk['path']}**　{usage} · " + disk_capacity(disk['used_bytes'])
                     + '/' + disk_capacity(disk['total_bytes']))
    else:
        lines.append('**磁盘 /**　暂不可用')
    timestamp = datetime.fromisoformat(metrics['sampled_at']).strftime('%m-%d %H:%M:%S')
    lines.append(f'{timestamp} · {REFRESH_SECONDS}秒刷新')
    if pin_state != 'automatic':
        lines.append('在操作菜单中选择“Pin”，即可从设置 → 应用 → Pin 查看。')
    return {'schema': '2.0', 'config': {'update_multi': True, 'summary': {'content': title},
                'style': {'text_size': {'normal_v2': {'default': 'normal', 'pc': 'normal', 'mobile': 'normal'}}}},
            'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': 'turquoise',
                       'padding': '8px 12px 8px 12px'},
            'body': {'padding': '8px 12px 8px 12px', 'elements': [
                {'tag': 'markdown', 'content': '\n'.join(lines), 'text_size': 'normal_v2',
                 'margin': '0px 0px 0px 0px'}]}}


class ApiError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__('Feishu API code=' + str(code))


class FeishuApi:
    def __init__(self, config):
        self.config, self.token, self.expiry = config, None, 0
        self.client = httpx.AsyncClient(base_url='https://open.feishu.cn', timeout=10)

    async def authenticate(self):
        if self.token and self.expiry > time.time():
            return self.token
        response = await self.client.post('/open-apis/auth/v3/tenant_access_token/internal',
            json={key: self.config[key] for key in ('app_id', 'app_secret')})
        payload = response.json()
        if payload.get('code') != 0:
            raise ApiError(payload.get('code'))
        response.raise_for_status()
        self.token = payload['tenant_access_token']
        self.expiry = time.time() + int(payload.get('expire', 7200)) - 120
        return self.token

    async def request(self, method, path, **kwargs):
        for attempt in range(3):
            token = await self.authenticate()
            try:
                response = await self.client.request(method, path,
                    headers={'Authorization': 'Bearer ' + token}, **kwargs)
                payload = response.json()
                code = payload.get('code')
                if code == 0:
                    response.raise_for_status()
                    return payload.get('data') or {}
                if code in AUTH_CODES:
                    self.expiry = 0
                elif code not in (11232, 99991400) and response.status_code < 500:
                    raise ApiError(code)
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise RuntimeError('Monitor network/response failure') from None
            if attempt == 2:
                raise ApiError(code)
            await asyncio.sleep(1 + attempt)

    async def send(self, chat_id, content, deduplication_id):
        result = await self.request('POST', '/open-apis/im/v1/messages',
            params={'receive_id_type': 'chat_id'},
            json={'receive_id': chat_id, 'msg_type': 'interactive', 'uuid': deduplication_id,
                  'content': json.dumps(content, ensure_ascii=False)})
        if not result.get('message_id'):
            raise RuntimeError('Monitor send response has no message ID.')
        return result['message_id']

    async def update(self, message_id, content):
        return await self.request('PATCH', '/open-apis/im/v1/messages/' + quote(message_id, safe=''),
            json={'content': json.dumps(content, ensure_ascii=False)})

    async def pin(self, chat_id, message_id):
        return await self.request('POST', '/open-apis/im/v1/pins',
            json={'message_id': message_id})


class Monitor:
    def __init__(self, api, settings, state_path):
        self.api, self.settings, self.state_path = api, settings, state_path
        self.state = read_private(state_path) if state_path.exists() else {}
        self.stop = asyncio.Event()

    def save(self):
        atomic_json(self.state_path, self.state)

    async def tick(self, metrics):
        if self.state.get('paused'):
            return
        now = time.time()
        previous_id = self.state.get('message_id')
        renew = bool(previous_id and now - self.state.get('created_at', now) >= RENEW_AFTER)
        if previous_id and not renew:
            try:
                await self.api.update(previous_id, card(metrics, self.state.get('pin_state'),
                                                       self.state.get('renewed', False)))
            except ApiError as exc:
                if exc.code in DELETED_CODES:
                    self.state.update(paused=True, pause_reason='monitor_message_deleted')
                    self.save()
                    LOG.info('monitor_paused reason=message_deleted')
                    self.stop.set()
                    return
                if exc.code != 230031:
                    raise
                renew = True
        if not previous_id or renew:
            # Freeze an old card before its edit deadline; never leave live-looking stale data.
            if renew and now - self.state.get('created_at', now) < 14 * 86400:
                retired = card(metrics)
                retired['header']['title']['content'] = '监控卡片已续期，请查看最新监控卡片'
                retired['config']['summary']['content'] = '监控卡片已续期'
                retired['body']['elements'] = [{'tag': 'markdown', 'content':
                    '此卡片已停止更新。请查看并置顶聊天中最新的服务器监控卡片。'}]
                try:
                    await self.api.update(previous_id, retired)
                except ApiError as exc:
                    if exc.code != 230031:
                        raise
            self.state.setdefault('pending_uuid', str(uuid.uuid4()))
            self.save()
            identifier = await self.api.send(self.settings['chat_id'], card(metrics, renewed=renew),
                                             self.state['pending_uuid'])
            self.state.update(message_id=identifier, created_at=now, renewed=renew,
                              pin_state='manual_required', pin_attempted=False)
            self.state.pop('pending_uuid', None)
            self.save()
            LOG.info('monitor_card_created renewed=%s', renew)
        if not self.state.get('pin_attempted'):
            try:
                await self.api.pin(self.settings['chat_id'], self.state['message_id'])
                self.state['pin_state'] = 'automatic'
                self.state.pop('pin_error_code', None)
            except ApiError as exc:
                self.state['pin_error_code'] = exc.code
                LOG.info('monitor_pin_manual_required code=%s', exc.code)
            # Record before the next refresh; no repeated permission prompts or new messages.
            self.state['pin_attempted'] = True
        self.state.update(last_update_at=metrics['sampled_at'],
                          update_count=self.state.get('update_count', 0) + 1,
                          latest_metrics=metrics, consecutive_failures=0)
        self.state.pop('last_error', None)
        self.save()

    async def run(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        while not self.stop.is_set() and not self.state.get('paused'):
            next_update = loop.time() + REFRESH_SECONDS
            try:
                await self.tick(await sample())
            except Exception as exc:
                # No tokens, message payloads, or SDK exception bodies in logs/state.
                error = 'api:' + str(exc.code) if isinstance(exc, ApiError) else type(exc).__name__
                self.state.update(last_error=error,
                                  consecutive_failures=self.state.get('consecutive_failures', 0) + 1)
                self.save()
                LOG.warning('monitor_refresh_failed error=%s', error)
            try:
                await asyncio.wait_for(self.stop.wait(), max(0, next_update - loop.time()))
            except asyncio.TimeoutError:
                pass


async def main_async(args):
    if args.sample:
        metrics = await sample()
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return
    settings = read_private(args.directory / 'server-monitor.json')
    if not settings.get('enabled'):
        return
    config = read_private(args.directory / 'config.json')
    api = FeishuApi(config)
    try:
        monitor = Monitor(api, settings, args.directory / 'server-monitor-state.json')
        if args.once:
            await monitor.tick(await sample())
            print(json.dumps({'update_count': monitor.state.get('update_count'),
                              'pin_state': monitor.state.get('pin_state'),
                              'pin_error_code': monitor.state.get('pin_error_code')}, ensure_ascii=False))
        else:
            await monitor.run()
    finally:
        await api.client.aclose()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--sample', action='store_true', help='Read local metrics only; no Feishu API calls.')
    parser.add_argument('--once', action='store_true', help='Create/refresh the single metrics card once.')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    lock = None
    try:
        if not args.sample:
            lock = (args.directory / '.server-monitor.lock').open('a')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(main_async(args))
    except BlockingIOError:
        print('Monitor already running.', file=__import__('sys').stderr)
        return 1
    except Exception as exc:
        LOG.error('monitor_failed type=%s', type(exc).__name__)
        return 1
    finally:
        if lock:
            lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

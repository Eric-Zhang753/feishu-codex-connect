#!/usr/bin/env python3
"""Register this bridge's existing commands in the native Feishu slash panel.

Defaults to a read-only plan. --apply creates/updates these names, preserves
other commands, and reads the result back. Credentials are loaded privately.
"""
import argparse
import ast
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
API = '/open-apis/application/v7/app_slash_commands'
COMMANDS = (
    ('sessions', '查询会话列表；翻页用 /sessions 页码'),
    ('use', '切换会话：/use 列表编号或完整会话ID'),
    ('current', '查看当前会话、项目和模型'),
    ('models', '查看可选模型及各模型支持的思考强度'),
    ('model', '切换模型：/model 名称或编号，可附带思考强度'),
    ('effort', '调整思考强度：/effort 强度或编号；不带参数查看选项'),
    ('status', '查看任务进度、排队和待回复请求'),
    ('mute', '静默当前私聊，仅接收 Codex 提问和审批'),
    ('unmute', '恢复当前私聊的机器人回复'),
    ('new', '下次任务使用新会话，保留原会话记录'),
    ('stop', '中止正在执行的任务，保留排队任务'),
    ('steer', '补充当前任务要求：/steer 补充内容'),
    ('goal', '开启或查看目标模式：/goal 目标内容；不带参数查看当前目标'),
    ('goal_edit', '修改当前目标：/goal_edit 新目标；暂停中的目标会保持暂停'),
    ('goal_pause', '暂停当前目标的自动续跑，保留目标及已有进度'),
    ('goal_resume', '继续当前暂停的目标，保留目标及已有用量'),
    ('goal_clear', '清除当前会话目标，结束该目标的自动续跑'),
    ('approve', '同意待审批操作：/approve 请求编号'),
    ('deny', '拒绝待审批操作：/deny 请求编号'),
    ('answer', '回答问题：/answer 请求编号 答案'),
    ('help', '查看全部命令和使用说明'),
)


def load_bridge():
    spec = importlib.util.spec_from_file_location('feishu_bridge_commands', ROOT / 'bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_supported():
    tree = ast.parse((ROOT / 'bridge.py').read_text())
    supported = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name) or node.left.id != 'command':
            continue
        for target in node.comparators:
            values = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
            supported.update(value.value.lstrip('/') for value in values
                             if isinstance(value, ast.Constant) and isinstance(value.value, str)
                             and value.value.startswith('/'))
    missing = {name for name, _ in COMMANDS} - supported
    if missing:
        raise RuntimeError('Commands not supported by installed bridge: ' + ', '.join(sorted(missing)))


def body_for(name, description):
    return {'command': name,
            'description': {'default_value': description, 'i18n': {'zh_cn': description}},
            'icon': {'icon_key': 'skill_outlined'}}


def matches(item, body):
    actual = item.get('description') or {}
    desired = body['description']
    return (actual.get('default_value') == desired['default_value']
            and (actual.get('i18n') or {}).get('zh_cn') == desired['i18n']['zh_cn']
            and (item.get('icon') or {}).get('icon_key') == body['icon']['icon_key'])


async def api(client, method, path=API, **kwargs):
    token = await client.authenticate()
    try:
        response = await client.client.request(method, path,
            headers={'Authorization': 'Bearer ' + token}, **kwargs)
    except Exception:
        # A write may have succeeded remotely. Rerun the list-first sync rather
        # than blindly retrying the creation after an uncertain transport result.
        raise RuntimeError(f'{method} slash commands: network failure; rerun to reconcile.') from None
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(f'{method} slash commands: invalid response (HTTP {response.status_code}).') from None
    if response.is_error or payload.get('code') != 0:
        code = payload.get('code')
        raise RuntimeError(f'{method} slash commands: Feishu code={code}, HTTP {response.status_code}.')
    return payload.get('data') or {}


async def list_commands(client):
    items, params, seen = [], {}, set()
    while True:
        data = await api(client, 'GET', params=params)
        page = data.get('items')
        if not isinstance(page, list):
            raise RuntimeError('Slash command list has an unexpected response structure.')
        items.extend(page)
        if not data.get('has_more'):
            break
        cursor = data.get('page_token') or data.get('next_page_token')
        if not cursor or cursor in seen:
            raise RuntimeError('Slash command pagination is incomplete; no changes made.')
        seen.add(cursor)
        params = {'page_token': cursor}
    return items


async def main(apply, config_path):
    os.umask(0o077)
    ensure_supported()
    bridge = load_bridge()
    directory = config_path.resolve().parent
    client = bridge.Feishu(bridge.load_config(config_path))
    try:
        before = await list_commands(client)
        existing = {item['command']: item for item in before}
        print('Existing native slash commands:', len(existing))
        plan = []
        for name, description in COMMANDS:
            body = body_for(name, description)
            previous = existing.get(name)
            action = 'create' if previous is None else ('keep' if matches(previous, body) else 'update')
            plan.append((action, name, body, previous))
            print(f'{action}: /{name} — {description}', flush=True)
        if not apply:
            print('Read-only plan complete. Pass --apply to register commands.')
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        bridge.atomic_json(directory / ('slash-commands-before-' + stamp + '.json'), {'items': before})
        for action, name, body, previous in plan:
            if action == 'create':
                await api(client, 'POST', json=body)
            elif action == 'update':
                patch = {key: value for key, value in body.items() if key != 'command'}
                # Preserve existing translations beyond the supplied Chinese one.
                translations = dict((previous.get('description') or {}).get('i18n') or {})
                translations.update(patch['description']['i18n'])
                patch['description']['i18n'] = translations
                await api(client, 'PATCH', API + '/' + quote(str(previous['command_id']), safe=''), json=patch)
            else:
                continue
            print(f'{action} succeeded: /{name}', flush=True)
            await asyncio.sleep(0.35)
        after = await list_commands(client)
        saved = {item['command']: item for item in after}
        invalid = [name for name, description in COMMANDS
                   if name not in saved or not matches(saved[name], body_for(name, description))]
        if invalid:
            raise RuntimeError('Read-back verification failed: ' + ', '.join(invalid))
        unrelated = set(existing) - {name for name, _ in COMMANDS}
        if any(saved.get(name) != existing[name] for name in unrelated):
            raise RuntimeError('An unrelated command changed during synchronization; inspect the saved snapshot.')
        receipt = {'verified_at': datetime.now(timezone.utc).isoformat(),
                   'managed_commands': [saved[name] for name, _ in COMMANDS],
                   'preserved_other_commands': len(unrelated)}
        bridge.atomic_json(directory / 'slash-commands.json', receipt)
        print('Verified registered commands:', len(COMMANDS))
        print('Other commands preserved:', len(unrelated))
    finally:
        await client.client.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=PROJECT_ROOT / 'runtime' / 'config.json')
    parser.add_argument('--apply', action='store_true', help='Create/update the managed command panel entries.')
    args = parser.parse_args()
    try:
        asyncio.run(main(args.apply, args.config))
    except Exception as exc:
        print('Command registration stopped: ' + str(exc), file=sys.stderr)
        sys.exit(1)

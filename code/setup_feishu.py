#!/usr/bin/env python3
"""扫码创建飞书机器人，或为当前机器人补齐桥接权限。"""
import argparse
import asyncio
import fcntl
import os
from pathlib import Path
import re
import shlex
import sys

import httpx

from bridge import Feishu, PROJECT_ROOT, atomic_json, load_config
from server_monitor import read_private

APP_ID = re.compile(r'cli_[A-Za-z0-9]+')
OPEN_ID = re.compile(r'ou_[A-Za-z0-9]+')


class SetupError(Exception):
    pass


def registration_addons(*, feedback, group, monitor):
    scopes = [
        'im:message.p2p_msg:readonly',
        'im:message:send_as_bot',
        'application:app_slash_command:read',
        'application:app_slash_command:write',
    ]
    if feedback:
        scopes.append('im:message.reactions:write_only')
    if group:
        scopes.append('im:message.group_at_msg:readonly')
    if monitor:
        # Updating an interactive card also accepts im:message:send_as_bot.
        scopes.append('im:message.pins:write_only')
    return {
        'preset': False,
        'scopes': {'tenant': scopes},
        'events': {'items': {'tenant': ['im.message.receive_v1']}},
        'callbacks': {'items': ['card.action.trigger']},
    }


async def register(addons, app_id=None):
    try:
        import lark_oapi
        from lark_oapi.scene.registration.errors import AppAccessDeniedError, AppExpiredError
        import qrcode
    except ImportError:
        raise SetupError('请先安装 requirements.lock 中的依赖。') from None

    def show_qr(info):
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(info['url'])
        print(f"请用手机飞书扫码并确认。二维码约 {info['expire_in']} 秒内有效。", flush=True)
        try:
            qr.print_ascii(tty=sys.stdout.isatty())
        except UnicodeEncodeError:
            pass
        print('也可以在飞书中打开下方链接。请在私有终端完成授权。', flush=True)
        print(info['url'], flush=True)

    def show_status(info):
        if info['status'] == 'domain_switched':
            raise SetupError('请选择中国版飞书账号重新扫码。')

    print('本次申请的飞书权限和订阅：', flush=True)
    for item in (*addons['scopes']['tenant'], *addons['events']['items']['tenant'],
                 *addons['callbacks']['items']):
        print('  ' + item, flush=True)

    options = {'app_id': app_id} if app_id else {
        'create_only': True,
        'app_preset': {'name': 'Codex Connect', 'desc': '通过飞书与 Linux 主机上的 Codex 对话'},
    }
    try:
        result = await lark_oapi.aregister_app(
            on_qr_code=show_qr, on_status_change=show_status,
            source='feishu-codex-connect', addons=addons, **options)
    except AppAccessDeniedError:
        raise SetupError('飞书授权未通过。请核对账号或组织的创建与授权规则。') from None
    except AppExpiredError:
        raise SetupError('飞书二维码已过期。若手机端已确认，请先检查飞书开放平台的应用状态。') from None

    if (result.get('user_info') or {}).get('tenant_brand') == 'lark':
        raise SetupError('请选择中国版飞书账号重新扫码。')
    return result


async def bot_info(config):
    client = Feishu(config)
    try:
        token = await client.authenticate()
        response = await client.client.get('/open-apis/bot/v3/info',
                                           headers={'Authorization': 'Bearer ' + token})
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict) and body.get('code') == 0 and isinstance(body.get('bot'), dict):
            return body['bot']
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TypeError):
        pass
    finally:
        await client.client.aclose()
    return {}


async def create_bot(path, project_dir, *, group, monitor):
    project = project_dir.expanduser().resolve()
    if not project.is_dir():
        raise SetupError('工作区必须是已存在的目录。')
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise SetupError('运行目录须归当前用户所有并设为 700。')

    lock = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('该运行目录已有一个扫码进程。') from None
        for existing in (path, directory / 'state.sqlite3', directory / 'pairing.json'):
            if existing.exists() or existing.is_symlink():
                raise SetupError('该目录已有桥接配置或状态。请补齐已有应用权限，或选择新的运行目录。')

        result = await register(registration_addons(feedback=True, group=group, monitor=monitor))
        app_id, secret = result.get('client_id'), result.get('client_secret')
        if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
            raise SetupError('飞书返回的应用 ID 无效。')
        if not isinstance(secret, str) or not secret.strip():
            raise SetupError('飞书未返回应用凭据。')

        config = {'app_id': app_id, 'app_secret': secret, 'project_dir': str(project),
                  'allowed_user_ids': [], 'message_feedback_enabled': True}
        try:
            bot = await bot_info(config)
            bot_id = bot.get('open_id')
            has_bot_id = isinstance(bot_id, str) and bool(OPEN_ID.fullmatch(bot_id))
            scanner = (result.get('user_info') or {}).get('open_id')
            # Both IDs use the ou_ prefix. The scanner's ID must differ from the bot's.
            if has_bot_id and scanner != bot_id and isinstance(scanner, str) and OPEN_ID.fullmatch(scanner):
                config['allowed_user_ids'] = [scanner]
            if group and has_bot_id:
                config['bot_open_id'] = bot_id
        finally:
            # Keep the issued credentials even if bot discovery is interrupted.
            atomic_json(path, config, overwrite=False)
            print(f'凭据已保存到 {path}。', flush=True)
    finally:
        os.close(lock)

    if config['allowed_user_ids']:
        print('已将扫码账号加入桥接白名单。')
    else:
        print('启动后请使用运行目录中的一次性绑定码完成私聊绑定。')
    if group and not has_bot_id:
        print('群聊权限已提交，机器人发布后请按部署教程填写 bot_open_id。')
    if bot.get('activate_status') != 2:
        print('飞书应用启用状态待确认，请按飞书页面提示完成发布或审批。')
    print('启动桥接后，向机器人发送 /help 验证私聊。')
    print(shlex.join([sys.executable, str(PROJECT_ROOT / 'code' / 'bridge.py'), '--config', str(path)]))


async def update_permissions(path, *, group, monitor):
    config = load_config(path)
    app_id = config['app_id']
    if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
        raise SetupError('配置中的飞书应用 ID 无效。')
    monitor_config = path.parent / 'server-monitor.json'
    if monitor_config.is_file():
        monitor = monitor or bool(read_private(monitor_config).get('enabled'))
    addons = registration_addons(feedback=config.get('message_feedback_enabled', True),
                                 group=group or bool(config.get('bot_open_id')), monitor=monitor)
    result = await register(addons, app_id=app_id)
    if result.get('client_id') != app_id:
        raise SetupError('飞书返回的应用与目标配置不一致。请核对扫码确认页。')
    print('飞书已确认权限更新。请按页面提示发布或提交审批，再验证消息与卡片按钮。')


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--project-dir', type=Path, help='新建应用，指定 Codex 初始工作区。')
    mode.add_argument('--update-permissions', action='store_true', help='为配置中的已有应用补齐权限。')
    parser.add_argument('--config', type=Path, default=PROJECT_ROOT / 'runtime' / 'config.json')
    parser.add_argument('--group', action='store_true', help='申请接收群聊 @机器人消息的权限。')
    parser.add_argument('--server-monitor', action='store_true', help='申请监控卡片置顶权限。')
    args = parser.parse_args(argv)
    path = args.config.expanduser()
    path = path.parent.resolve() / path.name
    try:
        if args.update_permissions:
            asyncio.run(update_permissions(path, group=args.group, monitor=args.server_monitor))
        else:
            asyncio.run(create_bot(path, args.project_dir, group=args.group, monitor=args.server_monitor))
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('已结束等待扫码。', file=sys.stderr)
        return 130
    except Exception as exc:
        # SDK and HTTP exceptions may contain the device URL, a token or credentials.
        print(f'飞书接入中止（{type(exc).__name__}）。请检查配置路径、权限及飞书应用状态。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Private Feishu <-> Codex bridge. Run --help for local diagnostics."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime
import hashlib
import hmac
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import secrets
import signal
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from codex_locator import find_codex
from question_channel import QuestionServer
from global_questions import GlobalQuestions
from message_feedback import MessageFeedback
from goal_commands import GoalCommands

LOG = logging.getLogger('codex-feishu')
HELP = ('直接发送文字即可提交任务，任务会依次执行。\n'
        '\n- `/sessions [页码]` 查询服务器上的会话\n- `/use 编号或会话ID` 切换会话\n- `/current` 查看当前会话（`/current id` 显示 ID）\n'
        '- `/models` 查看可选模型\n- `/model 名称或编号 [强度]` 修改模型\n'
        '- `/effort [强度]` 查看或修改思考强度\n'
        '- `/status` 查询状态\n- `/stop` 中止当前任务\n- `/new` 下次使用新会话\n'
        '- `/steer 补充要求` 调整正在执行的任务\n'
        '- `/goal 目标内容` 开启目标模式；`/goal` 查看目标\n'
        '- `/goal_edit 新目标` 修改目标\n'
        '- `/goal_pause` 暂停；`/goal_resume` 继续；`/goal_clear` 清除\n'
        '- `/approve 编号` 或 `/deny 编号` 回复审批\n'
        '- 点击问题卡片的选项，或 `/answer 请求编号 选项编号或答案` 回复问题\n'
        '- 多题可用 `/answer 请求编号 1;2` 按题目顺序选择\n- `/help` 查看帮助')


def atomic_json(path, data, *, overwrite=True):
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(4))
    try:
        with temporary.open('x', encoding='utf-8') as f:
            os.chmod(temporary, 0o600)
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def clean(text, config):
    text = str(text).replace(config['app_secret'], '[redacted]')
    text = re.sub(r'https://open\.feishu\.cn/open-apis/bot/v2/hook/[^\s)<>]+',
                  '[redacted webhook]', text)
    text = re.sub(r'(?i)Bearer\s+[A-Za-z0-9_.~+/-]+=*', 'Bearer [redacted]', text)
    return re.sub(r'<at\b', '&lt;at', text, flags=re.I)


def split_card_markdown(text, limit=3600):
    """Split by lines, keeping code fences balanced and repeating table headers.

    The character budget also bounds UTF-8/JSON payload size: control characters
    are removed before calling this function, and normal Unicode uses <=4 bytes.
    """
    if limit < 256:
        raise ValueError('Card chunk limit must be at least 256 characters.')
    lines = text.splitlines(keepends=True)
    chunks, buffer = [], ''
    fence, opener, table_header = '', '', ''
    prefix = ''
    fence_pattern = re.compile(r'^ {0,3}(`{3,}|~{3,})([^\n]*)$')
    table_separator = re.compile(r'^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$')

    def flush():
        nonlocal buffer, prefix
        if not buffer or buffer == prefix:
            return
        suffix = ('\n' if not buffer.endswith('\n') else '') + fence if fence else ''
        chunks.append(buffer + suffix)
        prefix = opener + '\n' if fence else table_header
        buffer = prefix

    index = 0
    while index < len(lines):
        unit = lines[index]
        next_table_header = None
        if not fence:
            if table_header and ('|' not in unit or not unit.strip()):
                table_header = ''
            if ('|' in unit and index + 1 < len(lines) and
                    table_separator.fullmatch(lines[index + 1].rstrip('\n'))):
                unit += lines[index + 1]
                next_table_header = unit
                index += 1
        match = fence_pattern.fullmatch(unit.rstrip('\n')) if next_table_header is None else None
        next_fence, next_opener = fence, opener
        if match:
            marker, info = match.groups()
            if not fence:
                # Extremely long fence markers/labels are treated as literal text.
                if len(unit) < limit // 4:
                    next_fence, next_opener = marker, unit.rstrip('\n')
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not info.strip():
                next_fence, next_opener = '', ''
        remaining = unit
        while remaining:
            reserve = len(next_fence) + 2
            available = limit - len(buffer) - reserve
            if len(remaining) <= available:
                buffer += remaining
                remaining = ''
            elif buffer and buffer != prefix:
                flush()
            else:
                # A single line can exceed a card's limit; always make progress.
                if available <= 0:
                    prefix, buffer, table_header = '', '', ''
                    available = limit - reserve
                buffer += remaining[:available]
                remaining = remaining[available:]
                flush()
        fence, opener = next_fence, next_opener
        if next_table_header is not None:
            table_header = next_table_header if len(next_table_header) < limit // 2 else ''
        index += 1
    flush()
    return chunks or ['（无正文）']


def notification_summary(text, config, limit=160):
    """Plain-text preview for Feishu card notifications and the chat list."""
    text = html.unescape(str(text))
    text = re.sub(r'<[^>\n]+>', '', text)
    text = re.sub(r'(?m)^\s*(?:`{3,}|~{3,})[^\n]*$', '', text)
    text = re.sub(r'!?\[([^\]\n]*)\]\([^\n)]*\)', r'\1', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|>\s*|[-+*]\s+)', '', text)
    text = re.sub(r'(?m)^\s*\|?[ :|\-]+\|?\s*$', '', text)
    text = text.replace('`', '').replace('**', '').replace('__', '').replace('~~', '')
    text = clean(text, config)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    text = re.sub(r'\s+', ' ', text).strip() or '（无正文）'
    return text if len(text) <= limit else text[:limit - 1] + '…'


def message_cards(text, config, *, notice=None):
    """Build inline Card JSON 2.0; no CardKit entity permissions are required."""
    text = clean(text, config).replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    if len(text) > 28000:
        text = text[:28000] + '\n\n…内容较长，其余请查看 Codex 会话。'
    chunks = split_card_markdown(text)
    cards = []
    for index, content in enumerate(chunks, 1):
        if config.get('compact_notifications'):
            # Notifications on this watch are clipped even for short body text.
            # Send a small explicit status; retain the full message in the card.
            title = notification_summary(notice or '新消息', config, limit=8)
            summary = title
        else:
            title = notification_summary(content, config, limit=60)
            title += f' · {index}/{len(chunks)}' if len(chunks) > 1 else ''
            summary = notification_summary(content, config)
        cards.append({'schema': '2.0', 'config': {'update_multi': True,
                'summary': {'content': summary}},
            'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': 'blue'},
            'body': {'elements': [{'tag': 'markdown', 'content': content}]}})
    return cards


def markdown_code(text, language='text'):
    text = str(text)
    longest = max((len(m.group()) for m in re.finditer(r'`+', text)), default=0)
    marker = '`' * max(3, longest + 1)
    return marker + language + '\n' + text + '\n' + marker


def question_answer(question, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('答案不能为空。')
    value = value.strip()
    options = question.get('options') or []
    if options and value.isdecimal():
        index = int(value) - 1
        if not 0 <= index < len(options):
            raise ValueError('选项编号超出范围。')
        return options[index]['label']
    return value


def question_cards(code, pending, config):
    questions = pending['params']['questions']
    result = []
    for index, q in enumerate(questions, 1):
        text = ('Codex 需要你的回答 · ' + str(index) + '/' + str(len(questions))
                + '\n\n' + q['question'])
        if pending.get('thread_id'):
            text += '\n\n会话：' + str(pending['thread_id'])[:8]
        for number, option in enumerate(q.get('options') or [], 1):
            text += '\n\n' + str(number) + '. ' + option['label']
            if option.get('description'):
                text += '：' + option['description']
        chosen = pending.get('answers', {}).get(q['id'])
        if chosen:
            text += '\n\n已选：' + chosen
        sample = ('1' if len(questions) == 1 else ';'.join('1' for _ in questions))
        if not q.get('options'):
            sample = '你的答案' if len(questions) == 1 else '{"问题ID":"答案"}'
        text += '\n\n可点击选项；也可发送：`/answer ' + code + ' ' + sample + '`'
        if len(questions) > 1:
            text += '\n\n全部题目回答后会一起提交。'
        cards = message_cards(text, config, notice='待回答')
        for number, option in enumerate(q.get('options') or [], 1):
            cards[-1]['body']['elements'].append({
                'tag': 'button', 'type': 'default',
                'text': {'tag': 'plain_text', 'content': clean(str(number) + '. ' + option['label'], config)},
                'behaviors': [{'type': 'callback', 'value': {
                    'action': 'codex_answer', 'code': code, 'question': q['id'], 'option': number}}]})
        result.extend(cards)
    return result


def load_config(path):
    if path.stat().st_mode & 0o077:
        raise RuntimeError('Config must use owner-only permissions (chmod 600).')
    config = json.loads(path.read_text())
    if not config.get('app_id') or not config.get('app_secret'):
        raise RuntimeError('App credentials are missing.')
    project = Path(config['project_dir']).resolve(strict=True)
    if not project.is_dir():
        raise RuntimeError('Project directory is invalid.')
    config['project_dir'] = str(project)
    return config


def normalize_event(raw, config):
    """Only current user text messages, with an explicit bot mention in groups."""
    header, event = raw.get('header', {}), raw.get('event', {})
    if header.get('app_id') not in (None, config['app_id']):
        return None
    sender, msg = event.get('sender', {}), event.get('message', {})
    user = (sender.get('sender_id') or {}).get('open_id')
    if sender.get('sender_type') != 'user' or not user or msg.get('message_type') != 'text':
        return None
    chat_type = msg.get('chat_type')
    if chat_type not in ('p2p', 'group') or not msg.get('chat_id') or not msg.get('message_id'):
        return None
    try:
        created = int(msg['create_time']) / 1000
        if abs(time.time() - created) > 300:
            return None
        content = json.loads(msg['content'])
        text = content['text']
        if not isinstance(text, str):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    mentions = msg.get('mentions') or []
    if chat_type == 'group':
        bot_id = config.get('bot_open_id')
        if not bot_id or not any((m.get('id') or {}).get('open_id') == bot_id for m in mentions):
            return None
    for mention in mentions:
        if (mention.get('id') or {}).get('open_id') == config.get('bot_open_id'):
            text = text.replace(mention.get('key', '\0'), '')
    text = text.strip()
    if not text or len(text) > 16000:
        return None
    return {'message_id': msg['message_id'], 'chat_id': msg['chat_id'], 'user_id': user,
            'chat_type': chat_type, 'text': text, 'created': created}


class State:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS chats (chat_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
            chat_type TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'received', result TEXT);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(chats)')}
        for name in ('cwd', 'model', 'model_provider', 'reasoning_effort'):
            if name not in columns:
                self.db.execute('ALTER TABLE chats ADD COLUMN ' + name + ' TEXT')
        self.db.commit()

    def setting(self, key):
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else None

    def bind(self, user):
        with self.lock, self.db:
            existing = self.setting('owner')
            if existing and existing != user:
                return False
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('owner', user))
            return True

    def set_setting(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))

    def receive(self, msg):
        with self.lock, self.db:
            cursor = self.db.execute('''INSERT OR IGNORE INTO messages
              (message_id,chat_id,user_id,chat_type,text,created) VALUES (?,?,?,?,?,?)''',
              tuple(msg[k] for k in ('message_id', 'chat_id', 'user_id', 'chat_type', 'text', 'created')))
            return cursor.rowcount == 1

    def mark(self, message_id, status, result=None):
        with self.lock, self.db:
            self.db.execute('UPDATE messages SET status=?,result=? WHERE message_id=?',
                            (status, result, message_id))

    def rows(self, status):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                'SELECT * FROM messages WHERE status=? ORDER BY created', (status,)).fetchall()]

    def thread(self, chat_id, thread_id=None):
        with self.lock, self.db:
            if thread_id is not None:
                self.db.execute('''INSERT INTO chats (chat_id,thread_id) VALUES (?,?)
                    ON CONFLICT(chat_id) DO UPDATE SET thread_id=excluded.thread_id''', (chat_id, thread_id))
            row = self.db.execute('SELECT thread_id FROM chats WHERE chat_id=?', (chat_id,)).fetchone()
            return (row[0] or None) if row else None

    def context(self, chat_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM chats WHERE chat_id=?', (chat_id,)).fetchone()
            return dict(row) if row else {}

    def selections(self):
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM chats WHERE thread_id<>'' ORDER BY chat_id")]

    def select_thread(self, chat_id, thread_id, cwd, model=None, model_provider=None, reasoning_effort=None):
        with self.lock, self.db:
            self.db.execute('''INSERT INTO chats (chat_id,thread_id,cwd,model,model_provider,reasoning_effort)
                VALUES (?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                thread_id=excluded.thread_id,cwd=excluded.cwd,model=excluded.model,
                model_provider=excluded.model_provider,reasoning_effort=excluded.reasoning_effort''',
                (chat_id, thread_id, cwd, model, model_provider, reasoning_effort))

    def set_model_settings(self, chat_id, model, effort):
        with self.lock, self.db:
            self.db.execute('''INSERT INTO chats (chat_id,thread_id,model,reasoning_effort)
                VALUES (?,'',?,?) ON CONFLICT(chat_id) DO UPDATE SET
                model=excluded.model,reasoning_effort=excluded.reasoning_effort''', (chat_id, model, effort))

    def new_thread(self, chat_id):
        with self.lock, self.db:
            self.db.execute("UPDATE chats SET thread_id='' WHERE chat_id=?", (chat_id,))


class Feishu:
    def __init__(self, config):
        self.config, self.token, self.expiry = config, None, 0
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(base_url='https://open.feishu.cn', timeout=15)

    async def authenticate(self):
        async with self.lock:
            if self.token and self.expiry > time.time():
                return self.token
            r = await self.client.post('/open-apis/auth/v3/tenant_access_token/internal',
                json={k: self.config[k] for k in ('app_id', 'app_secret')})
            r.raise_for_status()
            data = r.json()
            if data.get('code') != 0:
                raise RuntimeError(f"Feishu authentication code={data.get('code')}")
            self.token = data['tenant_access_token']
            self.expiry = time.time() + int(data.get('expire', 7200)) - 120
            return self.token

    async def send(self, chat_id, text, *, notice=None):
        return await self.send_cards(chat_id, message_cards(text, self.config, notice=notice))

    async def reaction_request(self, method, message_id, reaction_id=None, emoji=None):
        path = '/open-apis/im/v1/messages/' + quote(message_id, safe='') + '/reactions'
        if reaction_id:
            path += '/' + quote(reaction_id, safe='')
        for attempt in range(3):
            token = await self.authenticate()
            try:
                response = await self.client.request(method, path,
                    headers={'Authorization': 'Bearer ' + token}, timeout=5,
                    **({'json': {'reaction_type': {'emoji_type': emoji}}} if emoji else {}))
                payload = response.json()
                code = payload.get('code')
                if code == 0:
                    return payload.get('data') or {}
                if method == 'DELETE' and code in (231003, 231010, 231011):
                    return {}
                if code in (99991661, 99991663, 99991668):
                    self.expiry = 0
                elif code not in (11232, 99991400) and response.status_code < 500:
                    raise RuntimeError(f'Feishu reaction code={code}')
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise RuntimeError('Feishu reaction network/response failure') from None
            if attempt == 2:
                raise RuntimeError(f'Feishu reaction code={code}')
            await asyncio.sleep(1 + attempt)

    async def add_reaction(self, message_id, emoji):
        result = await self.reaction_request('POST', message_id, emoji=emoji)
        identifier = result.get('reaction_id')
        if not isinstance(identifier, str) or not identifier:
            raise RuntimeError('Feishu reaction response missing reaction_id')
        return identifier

    async def delete_reaction(self, message_id, reaction_id):
        await self.reaction_request('DELETE', message_id, reaction_id=reaction_id)

    async def send_cards(self, chat_id, cards):
        message_ids = []
        for card in cards:
            body = {'receive_id': chat_id, 'msg_type': 'interactive', 'uuid': str(uuid.uuid4()),
                    'content': json.dumps(card, ensure_ascii=False)}
            for attempt in range(3):
                try:
                    token = await self.authenticate()
                    r = await self.client.post('/open-apis/im/v1/messages',
                        params={'receive_id_type': 'chat_id'}, json=body,
                        headers={'Authorization': 'Bearer ' + token})
                    r.raise_for_status()
                    data = r.json()
                    code = data.get('code')
                    if code == 0:
                        message_id = (data.get('data') or {}).get('message_id')
                        if message_id:
                            message_ids.append(message_id)
                        break
                    if code in (99991661, 99991663, 99991668):
                        self.expiry = 0
                    elif code not in (11232, 99991400):
                        raise RuntimeError(f'Feishu send code={code}')
                    if attempt == 2:
                        raise RuntimeError(f'Feishu send code={code}')
                except (httpx.HTTPError, ValueError):
                    if attempt == 2:
                        raise RuntimeError('Feishu send network/response failure') from None
                await asyncio.sleep(1 + attempt)
        return message_ids


def codex_binary(explicit=None):
    return str(find_codex(explicit))


class Codex:
    def __init__(self, event_handler, shared_socket=None, *, runtime_dir=None,
                 codex_home=None, executable=None):
        self.handler, self.process, self.sequence = event_handler, None, 0
        self.shared_socket = shared_socket
        self.runtime_dir, self.codex_home, self.executable = runtime_dir, codex_home, executable
        self.pending, self.tasks = {}, set()
        self.start_lock = asyncio.Lock()

    async def start(self):
        async with self.start_lock:
            await self._start()

    async def _start(self):
        if self.process and self.process.returncode is None:
            return
        # This child replies through the app bot rather than duplicating the old webhook alert.
        env = dict(os.environ)
        if self.runtime_dir:
            env['CODEX_SHARED_ROOT'] = str(self.runtime_dir)
        if self.codex_home:
            env['CODEX_HOME'] = str(Path(self.codex_home).expanduser().resolve())
        arguments = ([sys.executable, str(ROOT / 'shared_backend.py'), '--proxy', self.shared_socket]
                     if self.shared_socket else
                     [codex_binary(self.executable) if self.executable else codex_binary(),
                      'app-server', '--listen', 'stdio://', '-c', 'notify=[]'])
        self.process = await asyncio.create_subprocess_exec(*arguments,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, limit=128 * 1024 * 1024)
        self.reader = asyncio.create_task(self.read())
        await self.request('initialize', {'clientInfo': {'name': 'feishu_codex_bridge',
            'title': 'Feishu Codex Bridge', 'version': '1.0.0'},
            'capabilities': {'experimentalApi': True}}, timeout=45)
        await self.write({'method': 'initialized', 'params': {}})
        LOG.info('codex_initialized')

    async def write(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
        await self.process.stdin.drain()

    async def request(self, method, params, timeout=90):
        self.sequence += 1
        identifier = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.write({'id': identifier, 'method': method, 'params': params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(identifier, None)

    async def respond(self, identifier, result=None, error=None):
        message = {'id': identifier}
        message['error' if error else 'result'] = error if error else result
        await self.write(message)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if 'id' in message and 'method' not in message:
                    future = self.pending.get(message['id'])
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(RuntimeError(str(message['error'].get('message', 'RPC error'))))
                        else:
                            future.set_result(message.get('result', {}))
                else:
                    task = asyncio.create_task(self.handler(message))
                    self.tasks.add(task)
                    def completed(future):
                        self.tasks.discard(future)
                        if not future.cancelled() and future.exception():
                            LOG.error('codex_event_handler_failed type=%s', type(future.exception()).__name__)
                    task.add_done_callback(completed)
        except Exception as exc:
            LOG.error('codex_stream_error type=%s', type(exc).__name__)
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError('Codex connection closed.'))
            await self.handler({'method': 'bridge/disconnected', 'params': {}})

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


class Bridge:
    def __init__(self, config, directory):
        self.config, self.directory = config, directory
        self.state = State(directory / 'state.sqlite3')
        self.shared = bool(config.get('shared_socket'))
        self.feishu, self.codex = Feishu(config), Codex(
            self.on_codex, config.get('shared_socket'), runtime_dir=directory,
            codex_home=config.get('codex_home'), executable=config.get('codex_binary'))
        self.feedback = MessageFeedback(self.feishu, self.state,
                                        enabled=config.get('message_feedback_enabled', True))
        self.incoming, self.jobs = asyncio.Queue(), asyncio.Queue()
        self.active, self.approvals = None, {}
        self.answer_lock = asyncio.Lock()
        self.thread_locks = {}
        self.goals = GoalCommands(self)
        self.global_questions = GlobalQuestions(self)
        self.loaded_threads, self.ws, self.ws_thread = set(), None, None
        self.observed, self.joining = {}, {}
        self.runtime_changed = asyncio.Event()
        self.connection_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.pair_path = directory / 'pairing.json'
        if not self.state.setting('owner') and not self.config.get('allowed_user_ids'):
            if not self.pair_path.exists() or json.loads(self.pair_path.read_text())['expires'] < time.time():
                atomic_json(self.pair_path, {'code': secrets.token_hex(8), 'expires': time.time() + 86400})

    def authorized(self, user):
        return user == self.state.setting('owner') or user in self.config.get('allowed_user_ids', [])

    def receive(self, raw):
        msg = normalize_event(raw, self.config)
        if msg and self.state.receive(msg):
            self.loop.call_soon_threadsafe(self.incoming.put_nowait, msg)

    async def reply(self, chat_id, text, *, notice=None):
        try:
            await self.feishu.send(chat_id, text, notice=notice)
            return True
        except Exception as exc:
            LOG.error('reply_failed type=%s detail=%s', type(exc).__name__, clean(str(exc), self.config)[:200])
            return False

    def private_question_chat(self):
        with self.state.lock:
            rows = self.state.db.execute("SELECT chat_id,user_id FROM messages WHERE chat_type='p2p' "
                                         'ORDER BY created DESC').fetchall()
        return next((row['chat_id'] for row in rows if self.authorized(row['user_id'])), None)

    async def show_question(self, code, pending):
        try:
            await self.feishu.send_cards(pending['chat_id'], question_cards(code, pending, self.config))
            return True
        except Exception as exc:
            LOG.warning('question_card_failed type=%s', type(exc).__name__)
            text = ['Codex 需要你的回答：']
            for q in pending['params']['questions']:
                text.append(q['id'] + '：' + q['question'])
                for number, option in enumerate(q.get('options') or [], 1):
                    text.append(str(number) + '. ' + option['label'])
            text.append('回复：`/answer ' + code + ' 选项编号或答案`；多题用分号分隔。')
            return await self.reply(pending['chat_id'], '\n\n'.join(text))

    async def register_question(self, event, chat, responder=None, source=None, code=None, display=True):
        params = event.get('params', {})
        questions = params.get('questions') or []
        if not questions or any(q.get('isSecret') for q in questions):
            return
        for existing_code, pending in self.approvals.items():
            if pending['rpc_id'] == event['id'] and pending.get('thread_id') == params.get('threadId'):
                if responder:
                    pending.update(respond=responder, source=source)
                if display and not pending.get('delivered'):
                    pending['delivered'] = bool(await self.show_question(existing_code, pending))
                return existing_code
        if not code or not re.fullmatch('[0-9a-f]{12}', code) or code in self.approvals:
            code = secrets.token_hex(6)
        pending = {'rpc_id': event['id'], 'params': params, 'kind': 'question',
                   'chat_id': chat, 'thread_id': params.get('threadId'),
                   'turn_id': params.get('turnId'), 'answers': {}, 'respond': responder, 'source': source,
                   'delivered': not display}
        self.approvals[code] = pending
        if display:
            pending['delivered'] = bool(await self.show_question(code, pending))
        return code

    async def relay_question(self, event, responder, source):
        if event.get('method') != 'item/tool/requestUserInput' or 'id' not in event:
            return
        chat = self.private_question_chat()
        if chat:
            await self.register_question(event, chat, responder, source)

    def resolve_question(self, params, source=None):
        for code, pending in list(self.approvals.items()):
            if (pending['rpc_id'] == params.get('requestId')
                    and pending.get('thread_id') == params.get('threadId')
                    and (source is None or pending.get('source') is source)):
                self.approvals.pop(code, None)

    def drop_question_source(self, source):
        for code, pending in list(self.approvals.items()):
            if pending.get('source') is source:
                self.approvals.pop(code, None)

    async def submit_question(self, code, pending):
        if not all(q['id'] in pending['answers'] for q in pending['params']['questions']):
            await self.reply(pending['chat_id'], '选择已记录，请继续回答其余题目。')
            return
        result = {'answers': {q['id']: {'answers': [pending['answers'][q['id']]]}
                              for q in pending['params']['questions']}}
        responder = pending.get('respond') or self.codex.respond
        await responder(pending['rpc_id'], result)
        self.approvals.pop(code, None)
        await self.reply(pending['chat_id'], '回答已提交给原 Codex 会话。')

    async def answer_text(self, chat, code, answer):
        async with self.answer_lock:
            pending = self.approvals.get(code)
            if not pending or pending['chat_id'] != chat or pending['kind'] != 'question':
                await self.reply(chat, '问题编号不存在、已结束，或不属于此聊天。')
                return
            questions = pending['params']['questions']
            try:
                if len(questions) == 1:
                    values = {questions[0]['id']: answer}
                elif answer.lstrip().startswith('{'):
                    values = json.loads(answer)
                    if not isinstance(values, dict):
                        raise ValueError('多题答案需要 JSON 对象。')
                else:
                    parts = re.split('[;；]', answer)
                    if len(parts) != len(questions):
                        raise ValueError('请按题目顺序用分号分隔，例如 1;2；也可用 JSON 回答。')
                    values = {q['id']: v for q, v in zip(questions, parts)}
                answers = {q['id']: question_answer(q, values[q['id']]) for q in questions}
            except (ValueError, TypeError, KeyError) as exc:
                await self.reply(chat, '答案格式不正确：' + (str(exc) if isinstance(exc, ValueError) else '请回答全部题目。'))
                return
            pending['answers'] = answers
            await self.submit_question(code, pending)

    def receive_card(self, raw):
        event = raw.get('event', {})
        user = (event.get('operator') or {}).get('open_id')
        context = event.get('context') or {}
        value = (event.get('action') or {}).get('value') or {}
        if not isinstance(value, dict) or value.get('action') != 'codex_answer':
            return '不支持此操作。', 'error'
        pending = self.approvals.get(value.get('code'))
        if (not self.authorized(user) or not pending or pending['kind'] != 'question'
                or pending['chat_id'] != context.get('open_chat_id')):
            return '问题已结束，或你无权回答此问题。', 'error'
        self.loop.call_soon_threadsafe(self.incoming.put_nowait, {
            '_card': True, 'user_id': user, 'chat_id': context['open_chat_id'], 'value': value})
        return '已接收选择，提交结果会显示在聊天中。', 'info'

    async def handle_card(self, msg):
        async with self.answer_lock:
            value = msg['value']
            code = value.get('code')
            pending = self.approvals.get(code)
            if (not self.authorized(msg['user_id']) or not pending or pending['kind'] != 'question'
                    or pending['chat_id'] != msg['chat_id']):
                return
            q = next((q for q in pending['params']['questions'] if q['id'] == value.get('question')), None)
            option = value.get('option')
            if not q or type(option) is not int or not 1 <= option <= len(q.get('options') or []):
                return
            pending.setdefault('answers', {})[q['id']] = q['options'][option - 1]['label']
            await self.submit_question(code, pending)

    async def ensure_codex(self):
        async with self.connection_lock:
            previous = self.codex.process
            await self.codex.start()
            if self.codex.process is not previous:
                self.loaded_threads.clear()
                self.observed.clear()
                if self.shared:
                    for row in self.state.selections():
                        thread = row['thread_id']
                        if thread in self.loaded_threads:
                            continue
                        try:
                            result = await self.resume_thread(thread)
                            self.loaded_threads.add(thread)
                            await self.refresh_runtime(thread, result['thread'])
                            if self.goals.is_managed(thread):
                                await self.goals.refresh(thread)
                        except Exception as exc:
                            LOG.warning('shared_subscription_restore_failed type=%s', type(exc).__name__)

    async def prepare_chat_thread(self, chat_id):
        async with self.thread_locks.setdefault(chat_id, asyncio.Lock()):
            await self.ensure_codex()
            params = {'cwd': self.project_for(chat_id), 'sandbox': 'workspace-write',
                      'approvalPolicy': 'on-request'}
            thread = self.state.thread(chat_id)
            if not thread:
                context = self.state.context(chat_id)
                if context.get('model'):
                    params['model'] = context['model']
                if context.get('model_provider'):
                    params['modelProvider'] = context['model_provider']
                overrides = {'notify': []} if self.shared else {}
                if context.get('reasoning_effort'):
                    overrides['model_reasoning_effort'] = context['reasoning_effort']
                result = await self.codex.request('thread/start', dict(params,
                    **({'config': overrides} if overrides else {}),
                    serviceName='feishu_codex_bridge', developerInstructions=self.config.get(
                        'developer_instructions', '用户通过飞书与你对话。按工作区 AGENTS.md 执行任务。'
                        '发送简明进度与结果，不回显密钥。需要额外权限时正常提出审批。')))
                thread = result['thread']['id']
                self.state.select_thread(chat_id, thread, str(result.get('cwd') or params['cwd']),
                    result.get('model') or result['thread'].get('model'),
                    result.get('modelProvider') or result['thread'].get('modelProvider'),
                    result.get('reasoningEffort', result['thread'].get('reasoningEffort')))
                self.loaded_threads.add(thread)
            elif thread not in self.loaded_threads:
                await self.read_thread(thread, require_idle=not self.shared)
                result = await self.resume_thread(thread)
                self.state.select_thread(chat_id, thread,
                    str(result.get('cwd') or result['thread'].get('cwd') or params['cwd']),
                    result.get('model') or result['thread'].get('model'),
                    result.get('modelProvider') or result['thread'].get('modelProvider'),
                    result.get('reasoningEffort', result['thread'].get('reasoningEffort')))
                self.loaded_threads.add(thread)
            return thread

    def chats_for(self, thread_id):
        chats = [row['chat_id'] for row in self.state.selections() if row['thread_id'] == thread_id]
        joining = self.joining.get(thread_id)
        if joining and joining not in chats:
            chats.append(joining)
        return chats

    def runtime_for(self, chat_id):
        thread_id = self.state.thread(chat_id)
        if self.active and (self.active['chat_id'] == chat_id or
                            (thread_id and self.active.get('thread_id') == thread_id)):
            if not self.active.get('waiting_external'):
                return self.active
        return self.observed.get(thread_id) or (
            self.active if self.active and self.active['chat_id'] == chat_id else None)

    def observe_turn(self, thread_id, turn):
        chats = self.chats_for(thread_id)
        if not chats:
            return None
        previous = self.observed.get(thread_id)
        if previous and previous.get('turn_id') == turn.get('id'):
            return previous
        runtime = {'chat_id': chats[0], 'thread_id': thread_id, 'turn_id': turn.get('id'),
                   'started': turn.get('startedAt') or time.time(), 'progress': '正在共享客户端中执行',
                   'final': '', 'file_changes': {}, 'last_progress': time.time(), 'mirrored': True}
        self.observed[thread_id] = runtime
        return runtime

    async def refresh_runtime(self, thread_id, thread=None):
        if thread is None:
            result = await self.codex.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
            thread = result['thread']
        if (thread.get('status') or {}).get('type') != 'active':
            return
        result = await self.codex.request('thread/turns/list', {'threadId': thread_id, 'limit': 1,
            'sortDirection': 'desc', 'itemsView': 'notLoaded'})
        for turn in result['data']:
            if turn.get('status') in ('inProgress', 'in_progress'):
                self.observe_turn(thread_id, turn)

    async def unsubscribe_unselected(self, thread_id):
        if not self.shared or not thread_id or self.chats_for(thread_id):
            return
        self.observed.pop(thread_id, None)
        self.approvals = {code: value for code, value in self.approvals.items()
                          if value.get('thread_id') != thread_id}
        if thread_id in self.loaded_threads:
            with suppress(Exception):
                await self.codex.request('thread/unsubscribe', {'threadId': thread_id})
            self.loaded_threads.discard(thread_id)

    async def wait_shared_idle(self, thread_id, chat_id):
        notified = False
        while self.shared:
            self.runtime_changed.clear()
            result = await self.codex.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
            if (result['thread'].get('status') or {}).get('type') != 'active':
                return
            await self.refresh_runtime(thread_id, result['thread'])
            if not notified:
                await self.reply(chat_id, '共享会话仍在执行上一项任务，本条任务会在它结束后开始。可用 /steer 补充当前任务要求。')
                notified = True
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.runtime_changed.wait(), 2)

    async def keep_shared_connection(self):
        while True:
            try:
                await self.ensure_codex()
                await self.recover_pending_use()
            except Exception as exc:
                LOG.warning('shared_reconnect_failed type=%s', type(exc).__name__)
            await asyncio.sleep(5)

    async def recover_pending_use(self):
        """Complete the owner's failed switch once the old IDE releases it."""
        path = self.directory / 'pending-use.json'
        if not path.exists() or self.active or self.state.rows('queued'):
            return
        intent = json.loads(path.read_text())
        if intent.get('expires', 0) < time.time() or not self.authorized(intent.get('user_id')):
            path.unlink(missing_ok=True)
            return
        try:
            await self.use_session(intent['chat_id'], intent['thread_id'])
        except RuntimeError as exc:
            if '另一个 Codex 实例占用' in str(exc):
                return
            intent.update(status='failed', detail=str(exc)[:500])
            atomic_json(self.directory / 'last-auto-use.json', intent)
            path.unlink(missing_ok=True)
            await self.reply(intent['chat_id'], '自动恢复会话切换失败：' + str(exc)[:500])
            return
        if self.state.thread(intent['chat_id']) == intent['thread_id']:
            intent.update(status='completed', completed_at=datetime.now().astimezone().isoformat())
            atomic_json(self.directory / 'last-auto-use.json', intent)
            path.unlink(missing_ok=True)

    def project_for(self, chat_id):
        return self.state.context(chat_id).get('cwd') or self.config['project_dir']

    def session_snapshot(self, chat_id):
        try:
            data = json.loads(self.state.setting('session-list:' + chat_id) or 'null')
            if isinstance(data, dict) and time.time() - data['created'] < 1800:
                return data
        except (ValueError, KeyError, TypeError):
            pass
        return None

    @staticmethod
    def thread_title(thread):
        text = thread.get('name') or thread.get('preview') or '未命名会话'
        return ' '.join(str(text).split())[:80]

    async def read_thread(self, thread_id, require_idle=False):
        await self.ensure_codex()
        result = await self.codex.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
        thread = result['thread']
        if require_idle:
            busy = (thread.get('status') or {}).get('type') == 'active'
            # Another app-server may report notLoaded even while VS Code is working.
            # Check the latest persisted turn as well, without resuming the thread.
            turns = await self.codex.request('thread/turns/list', {'threadId': thread_id,
                'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'})
            busy = busy or any(t.get('status') in ('inProgress', 'in_progress') for t in turns['data'])
            if busy:
                raise RuntimeError('目标会话仍在执行任务，请结束后再切换。')
        return thread

    async def resume_thread(self, thread_id):
        try:
            return await self.codex.request('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
        except RuntimeError as exc:
            if 'active writer' in str(exc).lower():
                advice = ('请重新打开 VS Code，让插件连接共享后端后再重试 /use。' if self.shared else
                          '请先在原实例退出该会话，再重试 /use。')
                raise RuntimeError('目标会话仍被另一个 Codex 实例占用。' + advice + '当前绑定未改变。') from None
            raise

    async def list_sessions(self, chat_id, argument):
        argument = argument.strip()
        if argument and (not argument.isdecimal() or not 1 <= int(argument) <= 100):
            await self.reply(chat_id, '用法：/sessions 或 /sessions 页码（1–100）。')
            return
        page = int(argument or '1')
        snapshot = self.session_snapshot(chat_id) if argument and page > 1 else None
        if not snapshot:
            snapshot = {'created': time.time(), 'threads': [], 'next_cursor': None, 'finished': False}
        await self.ensure_codex()
        requests = 0
        while len(snapshot['threads']) < page * 10 and not snapshot['finished']:
            requests += 1
            if requests > 100:
                raise RuntimeError('会话列表分页过多，请重新查询。')
            cursor = snapshot['next_cursor']
            result = await self.codex.request('thread/list', {'limit': 10, 'cursor': cursor,
                'sortKey': 'updated_at', 'sortDirection': 'desc', 'archived': False,
                'modelProviders': [], 'sourceKinds': ['cli', 'vscode', 'appServer', 'exec', 'unknown']})
            known = {t['id'] for t in snapshot['threads']}
            for thread in result['data']:
                if thread['id'] not in known and not thread.get('ephemeral'):
                    # Save metadata only; number-to-ID mappings survive a service restart.
                    snapshot['threads'].append({k: thread.get(k) for k in (
                        'id', 'name', 'preview', 'cwd', 'model', 'modelProvider', 'source', 'updatedAt')})
                    known.add(thread['id'])
            next_cursor = result.get('nextCursor')
            snapshot['finished'] = next_cursor is None
            if next_cursor is not None and next_cursor == cursor:
                raise RuntimeError('Codex 返回了重复的分页游标，请稍后重新查询。')
            snapshot['next_cursor'] = next_cursor
        self.state.set_setting('session-list:' + chat_id, json.dumps(snapshot, ensure_ascii=False))
        selected = snapshot['threads'][(page - 1) * 10:page * 10]
        if not selected:
            await self.reply(chat_id, '这一页没有会话。使用 /sessions 重新查询最近的会话。')
            return
        current = self.state.thread(chat_id)
        lines = [f'Codex 会话 · 第 {page} 页（按最近更新排序，未归档）']
        sources = {'vscode': 'VS Code', 'cli': '终端', 'appServer': '应用', 'exec': '脚本'}
        for number, thread in enumerate(selected, (page - 1) * 10 + 1):
            marker = '▶ ' if thread['id'] == current else ''
            project = Path(thread.get('cwd') or '/').name or '/'
            stamp = datetime.fromtimestamp(thread.get('updatedAt') or 0, ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
            source = thread.get('source')
            source = sources.get(source, '其他') if isinstance(source, str) else '其他'
            lines.extend([f'{number}. {marker}{self.thread_title(thread)}',
                          f'   项目：{project} · {source} · {stamp}'])
        lines.append('▶ 为当前会话。用 /use 编号 切换；编号在列表刷新前有效（最多 30 分钟）。')
        if not snapshot['finished'] or len(snapshot['threads']) > page * 10:
            lines.append(f'下一页：/sessions {page + 1}')
        await self.reply(chat_id, '\n'.join(lines))

    async def use_session(self, chat_id, argument):
        argument = argument.strip()
        if not argument:
            await self.reply(chat_id, '用法：先 /sessions 查看列表，再 /use 编号；也可 /use 完整会话ID。')
            return
        if self.active or self.state.rows('queued'):
            await self.reply(chat_id, '机器人仍有任务执行或排队，请结束后再切换会话。')
            return
        if argument.isdecimal():
            snapshot = self.session_snapshot(chat_id)
            index = int(argument) - 1
            if not snapshot or not 0 <= index < len(snapshot['threads']):
                await self.reply(chat_id, '会话编号不存在或列表已过期，请先 /sessions 查询对应页。')
                return
            thread_id = snapshot['threads'][index]['id']
        elif re.fullmatch(r'[A-Za-z0-9_-]{8,80}', argument):
            thread_id = argument
        else:
            await self.reply(chat_id, '请提供列表编号或完整会话ID。')
            return
        thread = await self.read_thread(thread_id, require_idle=not self.shared)
        cwd = Path(thread.get('cwd') or '').resolve(strict=True)
        if not thread.get('cwd') or not cwd.is_dir():
            raise RuntimeError('目标会话的项目目录不可用，当前绑定未改变。')
        # Preserve the target's model, working directory and permission settings.
        self.joining[thread_id] = chat_id
        try:
            result = ({'thread': thread} if thread_id in self.loaded_threads else
                      await self.resume_thread(thread_id))
        finally:
            self.joining.pop(thread_id, None)
        resumed = result['thread']
        if not self.shared and (resumed.get('status') or {}).get('type') == 'active':
            raise RuntimeError('目标会话正在执行，当前绑定未改变。')
        cwd = str(Path(result.get('cwd') or resumed.get('cwd') or str(cwd)).resolve(strict=True))
        model = result.get('model') or resumed.get('model') or thread.get('model')
        provider = result.get('modelProvider') or resumed.get('modelProvider') or thread.get('modelProvider')
        previous = self.state.thread(chat_id)
        self.state.select_thread(chat_id, thread_id, cwd, model, provider,
                                 result.get('reasoningEffort', resumed.get('reasoningEffort')))
        self.loaded_threads.add(thread_id)
        if self.shared:
            await self.refresh_runtime(thread_id, resumed)
            await self.unsubscribe_unselected(previous)
        await self.reply(chat_id, '已切换会话：' + self.thread_title(resumed or thread)
            + '\n项目：' + cwd + '\n模型：' + str(model or '未记录')
            + '\n之后的任务会继续此会话。')

    async def current_session(self, chat_id, argument=''):
        context = self.state.context(chat_id)
        thread_id = context.get('thread_id')
        if not thread_id:
            await self.reply(chat_id, '当前尚未选择会话。下一条任务会新建会话。\n项目：'
                + self.project_for(chat_id) + '\n也可 /sessions 查询后使用 /use 切换。')
            return
        thread = await self.read_thread(thread_id)
        model = thread.get('model') or context.get('model') or '未记录'
        provider = thread.get('modelProvider') or context.get('model_provider') or '未记录'
        status = (thread.get('status') or {}).get('type', 'unknown')
        labels = {'notLoaded': '未在本服务加载', 'idle': '空闲', 'active': '执行中', 'systemError': '异常'}
        effort = thread.get('reasoningEffort', context.get('reasoning_effort'))
        await self.reply(chat_id, '当前会话：' + self.thread_title(thread)
            + ('\nID：' + thread_id if argument.strip().lower() in ('id', '--id') else '')
            + '\n项目：' + str(thread.get('cwd') or self.project_for(chat_id))
            + '\n模型：' + str(model) + '\n思考强度：' + str(effort or '默认') + '\n提供方：' + str(provider)
            + '\n本服务状态：' + labels.get(status, status))

    async def available_models(self):
        await self.ensure_codex()
        models, seen, cursor = [], set(), None
        for _ in range(20):
            result = await self.codex.request('model/list', {'limit': 100, 'cursor': cursor, 'includeHidden': False})
            for model in result['data']:
                if model.get('model') and not model.get('hidden'):
                    models.append(model)
            cursor = result.get('nextCursor')
            if cursor is None:
                return models
            if cursor in seen:
                break
            seen.add(cursor)
        raise RuntimeError('模型列表分页异常，请稍后重试。')

    async def model_settings(self, chat_id):
        context = self.state.context(chat_id)
        thread_id = context.get('thread_id')
        if thread_id:
            thread = await self.read_thread(thread_id)
            return (thread.get('model') or context.get('model'),
                    thread.get('reasoningEffort', context.get('reasoning_effort')))
        await self.ensure_codex()
        result = await self.codex.request('config/read', {'cwd': self.project_for(chat_id), 'includeLayers': False})
        config = result['config']
        return (context.get('model') or config.get('model'),
                context.get('reasoning_effort') or config.get('model_reasoning_effort'))

    @staticmethod
    def model_options(model):
        return [item['reasoningEffort'] for item in model.get('supportedReasoningEfforts', [])]

    @staticmethod
    def find_model(models, name):
        name = str(name or '').lower()
        return next((item for item in models if name in (
            str(item.get('id', '')).lower(), str(item['model']).lower())), None)

    @staticmethod
    def resolve_effort(argument, options, default):
        value = argument.strip().lower()
        aliases = {'低': 'low', '中': 'medium', '高': 'high', '极高': 'xhigh',
                   '最大': 'max', '超强': 'ultra', '默认': 'default'}
        value = aliases.get(value, value)
        if value == 'default':
            value = default
        elif value.isdecimal():
            index = int(value) - 1
            value = options[index] if 0 <= index < len(options) else None
        return value if value in options else None

    async def list_models(self, chat_id):
        models = await self.available_models()
        self.state.set_setting('model-list:' + chat_id, json.dumps({
            'created': time.time(), 'models': [item['model'] for item in models]}))
        model, effort = await self.model_settings(chat_id)
        lines = ['当前模型：' + str(model or '默认') + ' · 思考强度：' + str(effort or '默认'), '', '可选模型：']
        for index, item in enumerate(models, 1):
            marker = '▶ ' if self.find_model([item], model) else ''
            lines.append(f"{index}. {marker}{item.get('displayName') or item['model']} · `{item['model']}`")
            options = self.model_options(item)
            if options:
                lines.append('   思考强度：' + ' / '.join(options))
        if not models:
            lines.append('当前没有返回可选模型。')
        lines.append('\n用 `/model 名称` 或 `/model 编号` 切换；编号在本列表刷新前有效（最多 30 分钟）。'
                     '\n也可 `/model 名称 强度` 一次设置两项。')
        await self.reply(chat_id, '\n'.join(lines))

    async def apply_model_settings(self, chat_id, model, effort):
        thread_id = self.state.thread(chat_id)
        if thread_id:
            if thread_id not in self.loaded_threads:
                await self.resume_thread(thread_id)
                self.loaded_threads.add(thread_id)
            params = {'threadId': thread_id, 'model': model}
            if effort is not None:
                params['effort'] = effort
            # This updates future turns and broadcasts the change to VS Code.
            # It never rewrites global config or permission/approval settings.
            await self.codex.request('thread/settings/update', params)
            thread = await self.read_thread(thread_id)
            actual_model = thread.get('model') or model
            actual_effort = thread.get('reasoningEffort', effort)
            self.state.set_model_settings(chat_id, actual_model, actual_effort)
            if actual_model != model or (effort is not None and actual_effort != effort):
                raise RuntimeError('设置已发生变化，请用 /current 查询当前值后重试。')
            suffix = '后续轮次生效。'
        else:
            self.state.set_model_settings(chat_id, model, effort)
            suffix = '下一次新建会话时生效。'
        await self.reply(chat_id, '模型：' + model + '\n思考强度：' + str(effort or '默认') + '\n' + suffix)

    async def change_model(self, chat_id, argument):
        values = argument.split()
        if not values:
            await self.list_models(chat_id)
            return
        if len(values) > 2:
            await self.reply(chat_id, '用法：/model 名称或编号 [思考强度]。用 /models 查看选项。')
            return
        name = values[0]
        if name.isdecimal():
            try:
                snapshot = json.loads(self.state.setting('model-list:' + chat_id) or 'null')
                index = int(name) - 1
                if not snapshot or time.time() - snapshot['created'] >= 1800 or not 0 <= index < len(snapshot['models']):
                    raise ValueError()
                name = snapshot['models'][index]
            except (ValueError, TypeError, KeyError):
                await self.reply(chat_id, '模型编号不存在或列表已过期，请先 /models 查询。')
                return
        models = await self.available_models()
        target = (next((item for item in models if item.get('isDefault')), None)
                  if name.lower() in ('default', '默认') else self.find_model(models, name))
        if target is None:
            await self.reply(chat_id, '模型不在当前可选列表中。请用 /models 查看准确名称。')
            return
        options = self.model_options(target)
        _, current_effort = await self.model_settings(chat_id)
        effort = current_effort if current_effort in options else target.get('defaultReasoningEffort')
        if len(values) == 2:
            effort = self.resolve_effort(values[1], options, target.get('defaultReasoningEffort'))
            if effort is None:
                await self.reply(chat_id, '此模型支持的思考强度：' + (' / '.join(options) or '不支持调整') + '。设置未改变。')
                return
        await self.apply_model_settings(chat_id, target['model'], effort if effort in options else None)

    async def change_effort(self, chat_id, argument):
        models = await self.available_models()
        name, current_effort = await self.model_settings(chat_id)
        target = self.find_model(models, name) if name else next((item for item in models if item.get('isDefault')), None)
        if target is None:
            await self.reply(chat_id, '当前模型不在可选列表中，请先用 /model 选择模型。')
            return
        options = self.model_options(target)
        if not argument.strip():
            lines = ['当前模型：' + target['model'], '思考强度：' + str(current_effort or '默认'), '']
            lines.extend(f'{i}. `{option}`' for i, option in enumerate(options, 1))
            lines.append('\n用 `/effort 强度或编号` 修改；`/effort default` 使用该模型的默认强度。'
                         if options else '此模型不支持调整思考强度。')
            await self.reply(chat_id, '\n'.join(lines))
            return
        effort = self.resolve_effort(argument, options, target.get('defaultReasoningEffort'))
        if effort is None:
            await self.reply(chat_id, '此模型支持的思考强度：' + (' / '.join(options) or '不支持调整') + '。设置未改变。')
            return
        await self.apply_model_settings(chat_id, target['model'], effort)

    async def handle(self, msg):
        text, chat, user = msg['text'], msg['chat_id'], msg['user_id']
        self.state.mark(msg['message_id'], 'handled')
        if not self.authorized(user):
            valid = False
            if text.startswith('/bind ') and msg['chat_type'] == 'p2p' and self.pair_path.exists():
                pairing = json.loads(self.pair_path.read_text())
                valid = (pairing['expires'] > time.time() and
                         hmac.compare_digest(text[6:].strip(), pairing['code']))
            if valid and self.state.bind(user):
                self.pair_path.unlink(missing_ok=True)
                await self.reply(chat, '账号已绑定，可以发送任务。\n项目：' + self.config['project_dir'] + '\n\n' + HELP)
                LOG.info('owner_bound')
            elif not self.state.setting('owner'):
                await self.reply(chat, '服务尚未绑定账号。请在此私聊发送控制台提供的 /bind 绑定码。')
            return
        parts = text.split(maxsplit=1)
        command, tail = parts[0], parts[1] if len(parts) > 1 else ''
        if command in ('/help', '/start'):
            await self.reply(chat, HELP)
        elif command == '/sessions':
            await self.list_sessions(chat, tail)
        elif command == '/use':
            (self.directory / 'pending-use.json').unlink(missing_ok=True)
            await self.use_session(chat, tail)
        elif command == '/current':
            await self.current_session(chat, tail)
        elif command == '/models':
            await self.list_models(chat)
        elif command == '/model':
            await self.change_model(chat, tail)
        elif command in ('/effort', '/reasoning'):
            await self.change_effort(chat, tail)
        elif command in ('/goal', '/goal_edit', '/goal_pause', '/goal_resume', '/goal_clear'):
            await self.goals.handle(msg, command, tail)
        elif command == '/status':
            active = self.runtime_for(chat)
            description = (f"执行中，已运行 {int((time.time() - active['started']) / 60)} 分钟\n"
                           + active.get('progress', '')[:600]) if active else '当前空闲'
            await self.reply(chat, f"项目：{self.project_for(chat)}\n{description}\n"
                f"排队任务：{len(self.state.rows('queued'))}\n待回复请求：{len(self.approvals)}",
                notice='执行中' if active else '空闲')
        elif command == '/stop':
            active = self.runtime_for(chat)
            if active and active.get('turn_id'):
                await self.codex.request('turn/interrupt', {'threadId': active['thread_id'],
                    'turnId': active['turn_id']})
                await self.reply(chat, '已请求中止当前任务；队列中的其他任务仍会依次执行。', notice='请求中止')
            else:
                await self.reply(chat, '当前没有可中止的任务。')
        elif command == '/new':
            (self.directory / 'pending-use.json').unlink(missing_ok=True)
            if self.runtime_for(chat) or any(
                    row['chat_id'] == chat for row in self.state.rows('queued')):
                await self.reply(chat, '此聊天仍有任务执行或排队，请结束后再新建会话。')
            else:
                previous = self.state.thread(chat)
                self.state.new_thread(chat)
                await self.unsubscribe_unselected(previous)
                await self.reply(chat, '下一条任务会使用新的 Codex 会话。')
        elif command == '/steer':
            active = self.runtime_for(chat)
            if active and active.get('turn_id') and tail.strip():
                await self.codex.request('turn/steer', {'threadId': active['thread_id'],
                    'expectedTurnId': active['turn_id'], 'input': [{'type': 'text', 'text': tail}]})
                await self.reply(chat, '补充要求已交给当前任务。')
            else:
                await self.reply(chat, '此聊天没有正在执行的任务，或补充要求为空。')
        elif command in ('/approve', '/deny', '/answer'):
            code, _, answer = tail.strip().partition(' ')
            if command == '/answer':
                await self.answer_text(chat, code, answer)
                return
            pending = self.approvals.get(code)
            if not pending or pending['chat_id'] != chat:
                await self.reply(chat, '请求编号不存在、已结束，或不属于此聊天。')
                return
            if command in ('/approve', '/deny') and pending['kind'] != 'question':
                accepted = command == '/approve'
                result = ({'permissions': pending['params']['permissions'] if accepted else {}, 'scope': 'turn'}
                          if pending['kind'] == 'permissions' else
                          {'decision': 'accept' if accepted else 'decline'})
            else:
                await self.reply(chat, '此请求需要使用 ' + ('/answer' if pending['kind'] == 'question' else '/approve 或 /deny'))
                return
            await self.codex.respond(pending['rpc_id'], result)
            self.approvals.pop(code, None)
            await self.reply(chat, '回复已提交。')
        elif command.startswith('/'):
            await self.reply(chat, '未识别的命令。\n' + HELP)
        else:
            self.state.mark(msg['message_id'], 'queued')
            await self.feedback.set(msg['message_id'], 'busy')
            self.jobs.put_nowait(msg)

    async def consume(self):
        while True:
            msg = await self.incoming.get()
            try:
                if msg.get('_card'):
                    await self.handle_card(msg)
                else:
                    await self.handle(msg)
            except Exception as exc:
                LOG.error('handle_failed type=%s', type(exc).__name__)
                await self.reply(msg['chat_id'], '请求处理失败：' + clean(str(exc), self.config)[:600])
            finally:
                self.incoming.task_done()

    async def worker(self):
        while True:
            msg = await self.jobs.get()
            if not self.authorized(msg['user_id']):
                self.state.mark(msg['message_id'], 'denied')
                await self.feedback.set(msg['message_id'], 'denied')
                self.jobs.task_done()
                continue
            self.state.mark(msg['message_id'], 'running')
            await self.feedback.set(msg['message_id'], 'busy')
            active = dict(msg, started=time.time(), progress='正在连接 Codex', final='', turn_id=None,
                          done=asyncio.get_running_loop().create_future(), last_progress=time.time(),
                          file_changes={}, waiting_external=self.shared, submitting=False)
            self.active = active
            try:
                thread = await self.prepare_chat_thread(msg['chat_id'])
                active['thread_id'] = thread
                if self.shared:
                    await self.wait_shared_idle(thread, msg['chat_id'])
                active['waiting_external'] = False
                active['submitting'] = True
                result = await self.codex.request('turn/start', {'threadId': thread,
                    'input': [{'type': 'text', 'text': msg['text']}],
                    'clientUserMessageId': msg['message_id']})
                active['turn_id'] = result['turn']['id']
                active['submitting'] = False
                turn = await active['done']
                status = turn.get('status')
                result_text = active['final'] or '本轮没有可显示的最终文字。'
                if status == 'completed':
                    title = 'Codex 本轮工作已结束'
                elif status == 'interrupted':
                    title = '任务已中止'
                else:
                    title = '任务执行失败'
                    result_text += '\n' + str((turn.get('error') or {}).get('message', '请查看 Codex 会话。'))
                self.state.mark(msg['message_id'], status or 'failed', clean(result_text, self.config)[:28000])
                notice = {'completed': '已完成', 'interrupted': '已中止'}.get(status, '执行失败')
                if status == 'completed' and self.goals.is_managed(thread):
                    notice = self.goals.notification_status(thread)
                await self.reply(msg['chat_id'], result_text if status == 'completed'
                                 else title + '\n\n' + result_text, notice=notice)
                await self.feedback.set(msg['message_id'], status or 'failed')
                LOG.info('task_finished status=%s', status)
            except Exception as exc:
                detail = clean(str(exc), self.config)[:1200]
                self.state.mark(msg['message_id'], 'failed', detail)
                await self.reply(msg['chat_id'], '任务未能完成：' + detail, notice='执行失败')
                await self.feedback.set(msg['message_id'], 'failed')
                LOG.error('task_failed type=%s', type(exc).__name__)
                # Only interrupt the Feishu-originated turn. Closing a shared
                # proxy must never terminate the backend or a VS Code turn.
                if self.shared and active.get('turn_id') and not active.get('waiting_external'):
                    with suppress(Exception):
                        await self.codex.request('turn/interrupt', {'threadId': active['thread_id'],
                            'turnId': active['turn_id']}, timeout=10)
                if not active['done'].done():
                    active['done'].cancel()
                await self.codex.close()
                self.loaded_threads.clear()
            finally:
                if self.shared:
                    self.approvals = {code: value for code, value in self.approvals.items()
                        if not (value.get('thread_id') == active.get('thread_id') and
                                value.get('turn_id') == active.get('turn_id'))}
                else:
                    self.approvals.clear()
                self.active = None
                self.jobs.task_done()

    async def on_codex(self, event):
        method, params = event.get('method'), event.get('params', {})
        thread_id = params.get('threadId')
        if method in ('thread/goal/updated', 'thread/goal/cleared'):
            self.goals.notice(params)
            return
        if method == 'thread/settings/updated':
            settings = params.get('threadSettings') or {}
            if settings.get('model'):
                for chat in self.chats_for(thread_id):
                    self.state.set_model_settings(chat, settings['model'], settings.get('effort'))
            return
        if method == 'serverRequest/resolved':
            self.resolve_question(params)
            return
        if method == 'bridge/disconnected':
            if self.active and not self.active['done'].done():
                self.active['done'].set_exception(RuntimeError('Codex connection closed; task was not retried.'))
            self.observed.clear()
            self.approvals.clear()
            self.loaded_threads.clear()
            self.runtime_changed.set()
            return
        if method == 'thread/closed':
            self.loaded_threads.discard(thread_id)
            self.observed.pop(thread_id, None)
            self.runtime_changed.set()
            return
        active = self.active
        if self.shared:
            own = bool(active and thread_id == active.get('thread_id') and
                       not active.get('waiting_external'))
            if own and params.get('turnId') and active.get('turn_id'):
                own = params['turnId'] == active['turn_id']
            if own and method in ('turn/started', 'turn/completed') and active.get('turn_id'):
                own = params.get('turn', {}).get('id') == active['turn_id']
            if not own:
                active = self.observed.get(thread_id)
                if method == 'turn/started':
                    active = self.observe_turn(thread_id, params['turn'])
        if not active:
            if 'id' in event and not self.shared:
                await self.codex.respond(event['id'], error={'code': -32601, 'message': 'No active user task'})
            return
        if params.get('threadId') and params['threadId'] != active.get('thread_id'):
            return
        if 'id' in event:
            kinds = {'item/commandExecution/requestApproval': 'command',
                     'item/fileChange/requestApproval': 'file',
                     'item/permissions/requestApproval': 'permissions',
                     'item/tool/requestUserInput': 'question'}
            kind = kinds.get(method)
            if kind is None:
                # VS Code may own frontend tools or attestation on the same
                # backend. Its client must be allowed to answer those requests.
                if not self.shared:
                    await self.codex.respond(event['id'], error={'code': -32601, 'message': 'Unsupported interactive request'})
                    await self.reply(active['chat_id'], '当前客户端不支持这个交互请求：' + str(method))
                return
            if kind == 'question':
                await self.register_question(event, active['chat_id'])
                return
            if any(pending['rpc_id'] == event['id'] for pending in self.approvals.values()):
                return
            code = secrets.token_hex(3)
            self.approvals[code] = {'rpc_id': event['id'], 'params': params, 'kind': kind,
                                    'chat_id': active['chat_id'], 'thread_id': thread_id,
                                    'turn_id': params.get('turnId') or active.get('turn_id')}
            details = (params.get('command') or params.get('permissions') or
                       active['file_changes'].get(params.get('itemId')) or params.get('grantRoot') or
                       params.get('itemId', ''))
            await self.reply(active['chat_id'], 'Codex 请求审批：\n原因：' + str(params.get('reason') or '请检查操作')
                + '\n目录：' + str(params.get('cwd') or self.config['project_dir'])
                + '\n\n操作：\n' + markdown_code(str(details)[:7000])
                + '\n\n同意：`/approve ' + code + '`\n\n拒绝：`/deny ' + code + '`', notice='待审批')
            return
        if method == 'turn/started':
            active['turn_id'] = params['turn']['id']
            self.runtime_changed.set()
        elif method == 'turn/completed':
            self.runtime_changed.set()
            if active.get('mirrored'):
                self.observed.pop(thread_id, None)
                self.approvals = {code: value for code, value in self.approvals.items()
                    if not (value.get('thread_id') == thread_id and
                            value.get('turn_id') == active.get('turn_id'))}
                turn = params['turn']
                final = active['final'] or '\n'.join(item.get('text', '') for item in turn.get('items', [])
                    if item.get('type') == 'agentMessage' and item.get('phase') in ('final_answer', None))
                title = {'completed': '共享会话本轮工作已结束', 'interrupted': '共享会话任务已中止'}.get(
                    turn.get('status'), '共享会话任务执行失败')
                if self.goals.is_managed(thread_id):
                    title = self.goals.progress_title(thread_id) if turn.get('status') == 'completed' else title
                if turn.get('error'):
                    final += '\n' + str(turn['error'].get('message') or '请查看 VS Code 会话。')
                notice = {'completed': '已完成', 'interrupted': '已中止'}.get(turn.get('status'), '执行失败')
                if turn.get('status') == 'completed' and self.goals.is_managed(thread_id):
                    notice = self.goals.notification_status(thread_id)
                for chat in self.chats_for(thread_id):
                    await self.reply(chat, title + '\n\n' + (final or '本轮没有可显示的最终文字。'), notice=notice)
            elif not active['done'].done():
                active['done'].set_result(params['turn'])
        elif method == 'item/completed':
            item = params.get('item', {})
            if item.get('type') == 'agentMessage':
                text = item.get('text', '')
                if item.get('phase') in ('final_answer', None):
                    active['final'] = text
                else:
                    active['progress'] = text[:1200]
                    if time.time() - active['last_progress'] > 90:
                        active['last_progress'] = time.time()
                        await self.reply(active['chat_id'], '执行进度：\n' + text[:1200], notice='执行中')
        elif method == 'item/started':
            item = params.get('item', {})
            if item.get('type') == 'commandExecution':
                active['progress'] = '正在运行：' + str(item.get('command', ''))[:1000]
            elif item.get('type') == 'fileChange':
                active['file_changes'][item['id']] = json.dumps(item.get('changes', []), ensure_ascii=False)

    def start_ws(self):
        def target():
            try:
                asyncio.set_event_loop(asyncio.new_event_loop())
                import lark_oapi as lark
                # SDK errors may include signed websocket URLs; retain no SDK request logs.
                logging.getLogger('Lark').disabled = True
                from lark_oapi.core.log import logger as sdk_log
                sdk_log.disabled = True
                def callback(data):
                    self.receive(json.loads(lark.JSON.marshal(data)))
                def card_callback(data):
                    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
                    text, kind = self.receive_card(json.loads(lark.JSON.marshal(data)))
                    return P2CardActionTriggerResponse({'toast': {'type': kind, 'content': text}})
                handler = (lark.EventDispatcherHandler.builder('', '')
                           .register_p2_im_message_receive_v1(callback)
                           .register_p2_card_action_trigger(card_callback).build())
                self.ws = lark.ws.Client(self.config['app_id'], self.config['app_secret'],
                    event_handler=handler, log_level=lark.LogLevel.ERROR)
                self.ws.start()
            except Exception as exc:
                LOG.error('feishu_ws_stopped type=%s', type(exc).__name__)
        self.ws_thread = threading.Thread(target=target, name='feishu-websocket', daemon=True)
        self.ws_thread.start()

    async def monitor(self):
        previous = None
        while True:
            connected = bool(self.ws and getattr(self.ws, '_conn', None) is not None)
            if connected != previous:
                LOG.info('feishu_ws_connected=%s', connected)
                previous = connected
            atomic_json(self.directory / 'status.json', {
                'updated_at': datetime.now().astimezone().isoformat(), 'pid': os.getpid(),
                'websocket_connected': connected, 'owner_bound': bool(self.state.setting('owner') or self.config.get('allowed_user_ids')),
                'project_dir': self.config['project_dir'], 'active': bool(self.active),
                'queued': len(self.state.rows('queued')), 'pending_requests': len(self.approvals),
                'shared_backend': self.shared, 'mirrored_active': len(self.observed),
                'global_questions': dict(self.global_questions.metrics),
                'message_feedback': dict(self.feedback.metrics),
                'codex_connected': bool(self.codex.process and self.codex.process.returncode is None)})
            if self.ws_thread and not self.ws_thread.is_alive():
                self.stop_event.set()
                return
            await asyncio.sleep(5)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, self.stop_event.set)
        await self.feishu.authenticate()
        self.question_server = QuestionServer(self, self.directory / 'question.sock')
        await self.question_server.start()
        await self.ensure_codex()
        interrupted = self.state.rows('running')
        for row in interrupted:
            self.state.mark(row['message_id'], 'interrupted', '服务重启，未自动重试。')
            await self.feedback.set(row['message_id'], 'interrupted')
            await self.reply(row['chat_id'], '服务重启，上一项任务已中断，未自动重试。请检查结果后决定是否继续。')
        for row in self.state.rows('queued'):
            self.jobs.put_nowait(row)
        for row in self.state.rows('received'):
            if time.time() - row['created'] < 300:
                self.incoming.put_nowait(row)
            else:
                self.state.mark(row['message_id'], 'expired')
        self.start_ws()
        tasks = [asyncio.create_task(self.consume()), asyncio.create_task(self.worker()),
                 asyncio.create_task(self.monitor()), asyncio.create_task(self.feedback.run())]
        if self.config.get('global_questions_enabled', False):
            tasks.append(asyncio.create_task(self.global_questions.run()))
        if self.shared:
            tasks.append(asyncio.create_task(self.keep_shared_connection()))
        LOG.info('bridge_started')
        await self.stop_event.wait()
        LOG.info('bridge_stopping')
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.codex.close()
        await self.question_server.close()
        await self.feishu.client.aclose()


async def check_codex(project, executable=None, codex_home=None):
    done = asyncio.get_running_loop().create_future()
    texts = []
    async def on_event(event):
        params = event.get('params', {})
        if 'id' in event:
            await client.respond(event['id'], error={'code': -32601, 'message': 'Diagnostic does not approve tools'})
        elif event.get('method') == 'item/completed' and params.get('item', {}).get('type') == 'agentMessage':
            texts.append(params['item'].get('text', ''))
        elif event.get('method') == 'turn/completed' and not done.done():
            done.set_result(params['turn'])
        elif event.get('method') == 'bridge/disconnected' and not done.done():
            done.set_exception(RuntimeError('Codex disconnected'))
    client = Codex(on_event, executable=executable, codex_home=codex_home)
    try:
        await client.start()
        result = await client.request('thread/start', {'cwd': project, 'ephemeral': True,
            'sandbox': 'read-only', 'approvalPolicy': 'on-request'})
        await client.request('turn/start', {'threadId': result['thread']['id'],
            'input': [{'type': 'text', 'text': '这是客户端连通性检查。不要读取文件，不要调用任何工具，只回复 FEISHU_BRIDGE_OK。'}]})
        turn = await asyncio.wait_for(done, 180)
        ok = turn['status'] == 'completed' and any('FEISHU_BRIDGE_OK' in t for t in texts)
        print('Codex model connectivity:', 'OK' if ok else 'FAILED')
        return 0 if ok else 1
    finally:
        await client.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=PROJECT_ROOT / 'runtime' / 'config.json')
    parser.add_argument('--check-codex', action='store_true')
    args = parser.parse_args()
    config = load_config(args.config)
    directory = args.config.resolve().parent
    handler = RotatingFileHandler(directory / 'service.log', maxBytes=2_000_000, backupCount=2)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    try:
        if args.check_codex:
            return asyncio.run(check_codex(config['project_dir'], config.get('codex_binary'),
                                           config.get('codex_home')))
        asyncio.run(Bridge(config, directory).run())
    except Exception as exc:
        LOG.error('bridge_failed type=%s detail=%s', type(exc).__name__, clean(str(exc), config)[:600])
        print('Bridge stopped:', type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

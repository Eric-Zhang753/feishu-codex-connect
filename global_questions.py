"""Discover genuine user-input calls across local Codex session rollouts.

Async questions are agent messages, rather than pending JSON-RPC approvals.
Their answers must be delivered as the IDE's structured user reply message.
"""
import asyncio
from contextlib import suppress
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time

from ide_ipc import IdeIpc

LOG = logging.getLogger('codex-feishu')
ASYNC = 'request_user_input_async'
BUILTIN = 'request_user_input'
REPLY = re.compile(r'<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>', re.S)


def question_id(call_id, index):
    return json.dumps([ASYNC, call_id, index], ensure_ascii=False, separators=(',', ':'))


def message_text(payload):
    content = payload.get('content') or []
    return '\n'.join(item.get('text', '') for item in content if isinstance(item, dict))


class Rollout:
    def __init__(self, path):
        self.path = Path(path)
        match = re.search(r'([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\.jsonl$', self.path.name)
        self.thread = match.group(1) if match else None
        self.offset = 0
        self.inode = None
        self.modified = None
        self.size = None
        self.questions = {}
        self.last_turn = None

    def consume(self, record):
        payload = record.get('payload') or {}
        if record.get('type') == 'session_meta':
            self.thread = payload.get('id') or self.thread
        if record.get('type') == 'event_msg' and payload.get('type') == 'task_started':
            self.last_turn = payload.get('turn_id')
        if record.get('type') != 'response_item':
            return
        kind = payload.get('type')
        if kind == 'message' and payload.get('role') == 'user':
            text = message_text(payload)
            match = REPLY.search(text)
            if match:
                try:
                    replies = json.loads(match.group(1))
                    if isinstance(replies, dict):
                        replies = [replies]
                    answered = {reply['questionItemId'] for reply in replies}
                    for call_id, question in list(self.questions.items()):
                        if question['name'] == ASYNC:
                            question['answered'].update(answered)
                            if all(question_id(call_id, i) in question['answered']
                                   for i in range(len(question['questions']))):
                                self.questions.pop(call_id, None)
                except (ValueError, TypeError, KeyError):
                    pass
            elif text.strip() and not text.lstrip().startswith((
                    '<external_codex_apps_open_page>', '<environment_context>', '<INSTRUCTIONS>')):
                # A subsequent ordinary user message supersedes old question UI.
                self.questions.clear()
        elif kind == 'function_call':
            name = payload.get('name', '').split('.')[-1]
            if name not in (ASYNC, BUILTIN):
                return
            try:
                args = payload.get('arguments') or '{}'
                args = json.loads(args) if isinstance(args, str) else args
                questions = args.get('questions') or []
                if not questions or any(q.get('isSecret') for q in questions):
                    return
                call_id = payload['call_id']
                self.questions[call_id] = {'name': name, 'questions': questions, 'answered': set(),
                    'call_id': call_id, 'turn_id': self.last_turn, 'timestamp': record.get('timestamp')}
            except (ValueError, TypeError, KeyError):
                return
        elif kind == 'function_call_output':
            call_id = payload.get('call_id')
            pending = self.questions.get(call_id)
            if pending and pending['name'] == BUILTIN:
                self.questions.pop(call_id, None)

    def update(self):
        info = self.path.stat()
        if self.inode != info.st_ino or info.st_size < self.offset:
            self.offset = 0
            self.questions.clear()
        self.inode = info.st_ino
        if info.st_mtime_ns == self.modified and info.st_size == self.size:
            return
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            while line := stream.readline():
                if not line.endswith(b'\n'):
                    break
                self.offset = stream.tell()
                try:
                    self.consume(json.loads(line))
                except (ValueError, TypeError):
                    continue
        self.modified = info.st_mtime_ns
        self.size = info.st_size


class GlobalQuestions:
    def __init__(self, bridge, sessions=None, ipc=None):
        self.bridge = bridge
        codex_home = Path(bridge.config.get('codex_home') or
                          os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
        self.sessions = Path(sessions) if sessions else codex_home / 'sessions'
        self.ipc = ipc or IdeIpc(codex_home)
        self.rollouts = {}
        self.registered = {}
        self.last_discovery = 0
        self.started = time.time()
        self.metrics = {'observed': 0, 'delivered': 0, 'answered': 0, 'last_error': None}

    def scan(self):
        now = time.time()
        if now - self.last_discovery >= 5:
            for path in self.sessions.rglob('*.jsonl'):
                try:
                    if path not in self.rollouts and path.stat().st_mtime >= self.started - 86400:
                        self.rollouts[path] = Rollout(path)
                except OSError:
                    continue
            self.last_discovery = now
        pending = {}
        for rollout in self.rollouts.values():
            try:
                rollout.update()
            except OSError:
                continue
            if rollout.thread:
                for call_id, item in rollout.questions.items():
                    pending[(rollout.thread, call_id)] = item
        return pending

    @staticmethod
    def saved_key(thread, call_id):
        return 'global-question:' + hashlib.sha256((thread + ':' + call_id).encode()).hexdigest()

    def saved(self, thread, call_id):
        value = self.bridge.state.setting(self.saved_key(thread, call_id))
        return json.loads(value) if value else {}

    def save(self, thread, call_id, **fields):
        current = self.saved(thread, call_id)
        current.update(fields, thread_id=thread, call_id=call_id)
        self.bridge.state.set_setting(self.saved_key(thread, call_id), json.dumps(current))

    async def register(self, thread, item):
        call_id = item['call_id']
        saved = self.saved(thread, call_id)
        if saved.get('status') in ('answered', 'resolved'):
            return
        chat = self.bridge.private_question_chat()
        if not chat:
            return
        if item['name'] == ASYNC:
            # Discover the live owner before notifying; historical offline chats
            # must not generate a flood of unanswerable cards at startup.
            await self.ipc.owner(thread)
            questions = []
            for index, original in enumerate(item['questions']):
                title = original.get('title') or original.get('question') or ''
                if not isinstance(title, str) or not title.strip():
                    return
                options = [{'label': option, 'description': ''} if isinstance(option, str) else option
                           for option in original.get('options') or []]
                questions.append({'id': question_id(call_id, index), 'header': 'Codex 提问',
                                  'question': title, 'options': options})
            event = {'id': 'async:' + call_id, 'method': 'item/tool/requestUserInput',
                     'params': {'threadId': thread, 'turnId': item['turn_id'],
                                'itemId': call_id, 'questions': questions, 'isBlocking': False}}
            async def respond(identifier, result):
                replies = [{'questionItemId': q['id'], 'question': q['question'],
                            'answer': '\n'.join(result['answers'][q['id']]['answers'])} for q in questions]
                await self.ipc.reply_async(thread, replies)
                self.save(thread, call_id, status='answered', answered_at=time.time())
                self.metrics['answered'] += 1
                LOG.info('global_question_answered thread=%s call=%s route=ide-ipc', thread, call_id)
        else:
            owner, snapshot = await self.ipc.snapshot(thread)
            try:
                request = next((r for r in snapshot.get('requests', [])
                                if r.get('method') == 'item/tool/requestUserInput'
                                and ((r.get('params') or {}).get('itemId') == call_id
                                     or [q.get('id') for q in (r.get('params') or {}).get('questions', [])]
                                     == [q.get('id') for q in item['questions']])), None)
            finally:
                await self.ipc.unfollow(thread, owner)
            if not request:
                return
            event = request
            async def respond(identifier, result):
                await self.ipc.reply_builtin(thread, identifier, result)
                self.save(thread, call_id, status='answered', answered_at=time.time())
                self.metrics['answered'] += 1
                LOG.info('global_question_answered thread=%s call=%s route=ide-ipc', thread, call_id)
        restored = saved.get('status') == 'pending' and bool(saved.get('code'))
        code = await self.bridge.register_question(event, chat, respond, self,
            code=saved.get('code') if restored else None, display=not restored)
        if code and (self.bridge.approvals.get(code) or {}).get('delivered'):
            self.registered[(thread, call_id)] = code
            self.save(thread, call_id, status='pending', code=code)
            self.metrics['delivered'] += 1
            LOG.info('global_question_delivered thread=%s call=%s async=%s restored=%s',
                     thread, call_id, item['name'] == ASYNC, restored)

    async def step(self):
        pending = await asyncio.to_thread(self.scan)
        self.metrics['observed'] = len(pending)
        for key, code in list(self.registered.items()):
            if key not in pending:
                if (self.bridge.approvals.get(code) or {}).get('source') is self:
                    self.bridge.approvals.pop(code, None)
                if self.saved(*key).get('status') != 'answered':
                    self.save(*key, status='resolved')
                self.registered.pop(key, None)
            elif code not in self.bridge.approvals:
                # A transport reconnect may clear the ordinary approval cache.
                # Recreate the same callback code without sending a duplicate card.
                self.registered.pop(key, None)
        for (thread, call_id), item in pending.items():
            if (thread, call_id) in self.registered:
                continue
            try:
                await self.register(thread, item)
                self.metrics['last_error'] = None
            except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
                self.metrics['last_error'] = str(exc)[:200]
        self.metrics['updated_at'] = datetime.now().astimezone().isoformat()

    async def run(self):
        try:
            while True:
                try:
                    await self.step()
                except Exception as exc:
                    LOG.error('global_question_monitor_failed type=%s', type(exc).__name__)
                    self.metrics['last_error'] = type(exc).__name__
                await asyncio.sleep(2)
        finally:
            await self.ipc.close()

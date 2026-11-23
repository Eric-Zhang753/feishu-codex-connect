"""Durable, nonblocking status reactions on the user's original message."""
import asyncio
from contextlib import suppress
import json
import logging
import time

LOG = logging.getLogger('codex-feishu')
EMOJI = {'busy': 'Typing', 'queued': 'Typing', 'running': 'Typing', 'completed': 'DONE',
         'failed': 'CrossMark', 'interrupted': 'CrossMark', 'denied': 'CrossMark', 'expired': 'CrossMark'}


class MessageFeedback:
    def __init__(self, feishu, state, enabled=True):
        self.feishu, self.state, self.enabled = feishu, state, enabled
        self.tasks = {}
        self.metrics = {'enabled': enabled, 'pending': 0, 'last_error': None}
        with state.lock, state.db:
            state.db.execute('''CREATE TABLE IF NOT EXISTS message_feedback (
                message_id TEXT PRIMARY KEY, desired TEXT NOT NULL, applied TEXT,
                reaction_id TEXT, stale_ids TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL)''')

    def row(self, message_id):
        with self.state.lock:
            row = self.state.db.execute('''SELECT f.*, m.chat_id FROM message_feedback f
                LEFT JOIN messages m ON m.message_id=f.message_id WHERE f.message_id=?''', (message_id,)).fetchone()
            return dict(row) if row else None

    def pending(self):
        with self.state.lock:
            return [message_id for message_id, chat_id in self.state.db.execute('''
                SELECT f.message_id, m.chat_id FROM message_feedback f
                LEFT JOIN messages m ON m.message_id=f.message_id
                WHERE f.applied IS NULL OR f.desired<>f.applied OR f.stale_ids<>'[]' ''').fetchall()
                if not chat_id or not self.state.chat_muted(chat_id)]

    async def set(self, message_id, status):
        if not self.enabled or status not in EMOJI:
            return
        with self.state.lock, self.state.db:
            chat = self.state.db.execute('SELECT chat_id FROM messages WHERE message_id=?', (message_id,)).fetchone()
            if chat and self.state.chat_muted(chat[0]):
                return
            self.state.db.execute('''INSERT INTO message_feedback(message_id,desired,updated) VALUES (?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET desired=excluded.desired,updated=excluded.updated''',
                (message_id, EMOJI[status], time.time()))
        self.schedule(message_id)

    def schedule(self, message_id):
        previous = self.tasks.get(message_id)
        if previous is None or previous.done():
            task = asyncio.create_task(self.reconcile(message_id))
            self.tasks[message_id] = task
            def finished(done):
                if self.tasks.get(message_id) is done:
                    self.tasks.pop(message_id, None)
            task.add_done_callback(finished)

    async def reconcile(self, message_id):
        try:
            for _ in range(4):
                row = self.row(message_id)
                if not row or (row['chat_id'] and self.state.chat_muted(row['chat_id'])):
                    return
                if row['desired'] != row['applied']:
                    # Adding a given emoji by the same app is idempotent. This
                    # also makes uncertain network outcomes safe to reconcile.
                    emoji = row['desired']
                    identifier = await self.feishu.add_reaction(message_id, emoji)
                    with self.state.lock, self.state.db:
                        current = self.row(message_id)
                        stale = json.loads(current['stale_ids'])
                        if current['reaction_id'] and current['reaction_id'] != identifier:
                            stale.append(current['reaction_id'])
                        self.state.db.execute('''UPDATE message_feedback SET applied=?,reaction_id=?,
                            stale_ids=?,updated=? WHERE message_id=?''',
                            (emoji, identifier, json.dumps(list(dict.fromkeys(stale))), time.time(), message_id))
                    LOG.info('message_feedback_applied emoji=%s', emoji)
                current = self.row(message_id)
                for identifier in json.loads(current['stale_ids']):
                    await self.feishu.delete_reaction(message_id, identifier)
                    with self.state.lock, self.state.db:
                        latest = self.row(message_id)
                        stale = [value for value in json.loads(latest['stale_ids']) if value != identifier]
                        self.state.db.execute('UPDATE message_feedback SET stale_ids=?,updated=? WHERE message_id=?',
                                              (json.dumps(stale), time.time(), message_id))
                current = self.row(message_id)
                if current['desired'] == current['applied'] and current['stale_ids'] == '[]':
                    self.metrics['last_error'] = None
                    return
        except Exception as exc:
            self.metrics['last_error'] = str(exc)[:160]
            LOG.warning('message_feedback_deferred type=%s', type(exc).__name__)
        finally:
            self.metrics['pending'] = len(self.pending())

    def recover(self):
        with self.state.lock, self.state.db:
            rows = self.state.db.execute('''SELECT f.message_id,m.status FROM message_feedback f
                JOIN messages m ON m.message_id=f.message_id''').fetchall()
            for message_id, status in rows:
                if status in EMOJI:
                    self.state.db.execute('UPDATE message_feedback SET desired=? WHERE message_id=?',
                                          (EMOJI[status], message_id))

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def run(self):
        try:
            if not self.enabled:
                await asyncio.Future()
            self.recover()
            while True:
                pending = self.pending()
                self.metrics['pending'] = len(pending)
                for message_id in pending:
                    self.schedule(message_id)
                await asyncio.sleep(15)
        finally:
            await self.close()

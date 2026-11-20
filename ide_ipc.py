"""Use the IDE's existing private IPC router to reach a thread's real owner."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import stat
import struct
import uuid


class IdeIpc:
    def __init__(self, codex_home, path=None):
        self.path = Path(path) if path else Path(codex_home) / 'ipc' / 'ipc.sock'
        self.writer = self.reader_task = None
        self.client_id = 'initializing-client'
        self.pending = {}
        self.snapshots = {}
        self.snapshot_waiters = {}
        self.lock = asyncio.Lock()

    async def connect(self):
        async with self.lock:
            if self.writer and not self.writer.is_closing() and self.client_id != 'initializing-client':
                return
            info = self.path.lstat()
            parent = self.path.parent.lstat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
                raise RuntimeError('IDE IPC must use a private user-owned socket.')
            reader, self.writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.path)), 3)
            self.reader_task = asyncio.create_task(self.read(reader, self.writer))
            reply = await self.request('initialize', {'clientType': 'feishu-question-bridge'}, version=0, connect=False)
            self.client_id = reply['result']['clientId']

    async def send(self, message):
        payload = json.dumps(message, ensure_ascii=False).encode()
        self.writer.write(struct.pack('<I', len(payload)) + payload)
        await self.writer.drain()

    async def read(self, reader, writer):
        try:
            while True:
                length = struct.unpack('<I', await reader.readexactly(4))[0]
                if not 0 < length <= 64 * 1024 * 1024:
                    raise ValueError('Invalid IDE IPC frame length.')
                reply = json.loads(await reader.readexactly(length))
                if reply.get('type') == 'response':
                    future = self.pending.get(reply.get('requestId'))
                    if future and not future.done():
                        if reply.get('resultType') == 'error':
                            future.set_exception(RuntimeError(reply.get('error', 'IDE IPC request failed.')))
                        else:
                            future.set_result(reply)
                elif reply.get('type') == 'client-discovery-request':
                    await self.send({'type': 'client-discovery-response', 'requestId': reply['requestId'],
                                     'response': {'canHandle': False}})
                elif reply.get('type') == 'broadcast' and reply.get('method') == 'thread-stream-state-changed':
                    params = reply.get('params') or {}
                    change = params.get('change') or {}
                    if change.get('type') == 'snapshot':
                        thread = params.get('conversationId')
                        self.snapshots[thread] = change.get('conversationState') or {}
                        waiter = self.snapshot_waiters.get(thread)
                        if waiter and not waiter.done():
                            waiter.set_result(self.snapshots[thread])
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            if writer is self.writer:
                self.client_id = 'initializing-client'
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError('IDE IPC connection closed.'))

    async def request(self, method, params, version=1, target=None, connect=True):
        if connect:
            await self.connect()
        identifier = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.send({'type': 'request', 'requestId': identifier, 'sourceClientId': self.client_id,
                             'version': version, 'method': method, 'params': params,
                             'targetClientId': target, 'timeoutMs': 10000})
            return await asyncio.wait_for(future, 12)
        finally:
            self.pending.pop(identifier, None)

    async def owner(self, thread):
        result = await self.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': thread})
        return result['handledByClientId']

    async def snapshot(self, thread):
        owner = await self.owner(thread)
        waiter = asyncio.get_running_loop().create_future()
        self.snapshot_waiters[thread] = waiter
        try:
            for following in (False, True):
                await self.send({'type': 'broadcast', 'method': 'thread-stream-following-changed', 'version': 1,
                                 'sourceClientId': self.client_id, 'targetClientIds': [owner],
                                 'params': {'conversationId': thread, 'hostId': 'local', 'following': following}})
            state = await asyncio.wait_for(waiter, 5)
            return owner, state
        finally:
            self.snapshot_waiters.pop(thread, None)

    async def unfollow(self, thread, owner):
        with suppress(OSError, RuntimeError):
            await self.send({'type': 'broadcast', 'method': 'thread-stream-following-changed', 'version': 1,
                             'sourceClientId': self.client_id, 'targetClientIds': [owner],
                             'params': {'conversationId': thread, 'hostId': 'local', 'following': False}})
        self.snapshots.pop(thread, None)

    async def reply_async(self, thread, replies):
        owner = await self.owner(thread)
        text = '<send_user_message_question_reply>\n' + json.dumps(replies, ensure_ascii=False) + '\n</send_user_message_question_reply>'
        identifier = str(uuid.uuid4())
        inputs = [{'type': 'text', 'text': text, 'text_elements': []}]
        restore = {'id': identifier, 'text': text, 'context': {}}
        try:
            return await self.request('thread-follower-steer-turn', {
                'conversationId': thread, 'input': inputs, 'restoreMessage': restore,
                'clientUserMessageId': identifier, 'attachments': []}, target=owner)
        except RuntimeError as exc:
            # Retry as a new turn only after an explicit rejection for inactivity.
            # An uncertain transport outcome is never retried as another message.
            detail = str(exc).lower()
            if not any(x in detail for x in ('not being streamed', 'no active turn', 'not in progress',
                                             'no active in-progress', 'active turn already ended', 'noactiveturn')):
                raise
        return await self.request('thread-follower-start-turn', {
            'conversationId': thread, 'turnStart': {
                'request': {'threadId': thread, 'input': inputs, 'clientUserMessageId': identifier},
                'context': {'restoreMessage': restore, 'attachments': [], 'commentAttachments': []}}},
            version=2, target=owner)

    async def reply_builtin(self, thread, request_id, response):
        owner = await self.owner(thread)
        return await self.request('thread-follower-submit-user-input', {
            'conversationId': thread, 'requestId': request_id, 'response': response}, target=owner)

    async def close(self):
        if self.reader_task:
            self.reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader_task
        if self.writer:
            self.writer.close()

"""Private local channel for IDE questions and their Feishu answers.

The IDE proxy owns the original JSON-RPC connection. Passing answers back
through that connection also works for threads not selected in the bot.
"""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import stat
import struct

LIMIT = 1024 * 1024


def channel_path():
    return Path('/tmp') / ('codex-feishu-questions-' + str(os.getuid())) / 'bridge.sock'


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Question channel directory must be private and owned by this user.')


async def write(writer, message):
    writer.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
    await writer.drain()


class QuestionServer:
    def __init__(self, bridge, path=None):
        self.bridge = bridge
        self.path = Path(path) if path else channel_path()
        self.clients = set()

    async def start(self):
        private_directory(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('Refusing to replace an unexpected question channel path.')
            # Never unlink a live server belonging to another bridge instance.
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                self.path.unlink()
            else:
                raise RuntimeError('A question channel server is already running.')
            finally:
                probe.close()
        self.server = await asyncio.start_unix_server(self.handle, path=str(self.path), limit=LIMIT)
        os.chmod(self.path, 0o600)

    async def handle(self, reader, writer):
        peer = writer.get_extra_info('socket')
        _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            writer.close()
            return
        self.clients.add(writer)
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if message.get('type') == 'question':
                    request = message['request']
                    async def respond(identifier, result, target=writer):
                        await write(target, {'type': 'answer', 'id': identifier, 'result': result})
                    await self.bridge.relay_question(request, respond, writer)
                elif message.get('type') == 'resolved':
                    self.bridge.resolve_question(message['params'], writer)
        except (OSError, ValueError, KeyError, asyncio.LimitOverrunError):
            pass
        finally:
            self.bridge.drop_question_source(writer)
            self.clients.discard(writer)
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in list(self.clients):
            writer.close()
        self.path.unlink(missing_ok=True)


class QuestionClient:
    def __init__(self, send_answer, path=None):
        self.send_answer = send_answer
        self.path = Path(path) if path else channel_path()
        self.writer = self.reader_task = None
        self.pending = {}
        self.lock = asyncio.Lock()

    async def connect(self):
        if self.writer and not self.writer.is_closing():
            return
        private_directory(self.path.parent)
        info = self.path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RuntimeError('Question channel must be a private user-owned socket.')
        reader, self.writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.path), limit=LIMIT), 1)
        self.reader_task = asyncio.create_task(self.read(reader))

    async def read(self, reader):
        try:
            while line := await reader.readline():
                reply = json.loads(line)
                if reply.get('type') == 'answer' and reply.get('id') in self.pending:
                    await self.send_answer({'id': reply['id'], 'result': reply['result']})
                    self.pending.pop(reply['id'], None)
        except (OSError, ValueError, KeyError):
            pass
        finally:
            self.pending.clear()
            if self.writer:
                self.writer.close()

    async def observe(self, message):
        async with self.lock:
            try:
                if message.get('method') == 'item/tool/requestUserInput' and 'id' in message:
                    # Secret inputs belong in the original trusted UI.
                    if any(q.get('isSecret') for q in message.get('params', {}).get('questions', [])):
                        return
                    await self.connect()
                    self.pending[message['id']] = message
                    await write(self.writer, {'type': 'question', 'request': message})
                    return True
                elif message.get('method') == 'serverRequest/resolved' and self.writer:
                    self.pending.pop(message['params'].get('requestId'), None)
                    await write(self.writer, {'type': 'resolved', 'params': message['params']})
            except (OSError, RuntimeError, asyncio.TimeoutError):
                self.pending.clear()
                if self.writer:
                    self.writer.close()
        return False

    async def close(self):
        if self.reader_task:
            self.reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader_task
        if self.writer:
            self.writer.close()

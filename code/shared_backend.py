#!/usr/bin/env python3
"""VS Code stdio launcher and systemd entry point for the shared Codex backend."""
import asyncio
from contextlib import suppress
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from codex_locator import find_codex

ROOT = Path(os.environ.get('CODEX_SHARED_ROOT', Path(__file__).resolve().parent.parent / 'runtime')).expanduser().resolve()
# Long conversation snapshots can exceed 16 MiB. Use the same bounded budget
# in both directions; never truncate the JSONL or its UTF-8 contents.
MAX_MESSAGE_BYTES = 128 * 1024 * 1024


def load_runtime():
    path = ROOT / 'shared-runtime.json'
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Shared runtime configuration must be private to this user.')
    return json.loads(path.read_text())


def real_binary(runtime):
    return find_codex(runtime.get('codex_binary'))


def is_stdio_app_server(arguments):
    """Intercept only normal app-server startup, never exec prompts or tooling."""
    index = 0
    with_value = {'-c', '--config', '--enable', '--disable', '-p', '--profile', '-C', '--cd'}
    while index < len(arguments):
        arg = arguments[index]
        if arg in with_value:
            index += 2
            continue
        if arg.startswith('-'):
            index += 1
            continue
        if arg != 'app-server':
            return False
        tail = arguments[index + 1:]
        if any(arg in ('proxy', 'daemon', 'generate-ts', 'generate-json-schema', 'help', '-h', '--help')
               for arg in tail):
            return False
        for offset, value in enumerate(tail):
            if value == '--listen' and offset + 1 < len(tail) and tail[offset + 1] != 'stdio://':
                return False
            if value.startswith('--listen=') and value != '--listen=stdio://':
                return False
        return True
    return False


def wait_for_backend(runtime):
    if runtime.get('manage_service', True):
        result = subprocess.run(['systemctl', '--user', 'start', runtime['service']],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            raise RuntimeError('Shared Codex service could not be started; inspect its user service status.')
    path = Path(runtime['socket'])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            info = path.stat()
            parent = path.parent.stat()
            if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                    or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
                raise RuntimeError('Shared Codex socket ownership or permissions are unsafe.')
            return
        except FileNotFoundError:
            time.sleep(0.1)
    raise RuntimeError('Shared Codex service did not create its local control socket.')


async def relay_stdio(socket, relay_questions=True):
    """Adapt IDE JSONL stdio to the app-server Unix WebSocket transport.

    The CLI's built-in `app-server proxy` uses its daemon control protocol;
    `--listen unix://PATH` instead expects an HTTP WebSocket Upgrade handshake.
    """
    from websockets.asyncio.client import unix_connect
    from websockets.exceptions import ConnectionClosedOK
    from question_channel import QuestionClient
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    try:
        async with unix_connect(socket, uri='ws://localhost', max_size=MAX_MESSAGE_BYTES,
                                max_queue=4, open_timeout=15, ping_interval=20, ping_timeout=30) as ws:
            async def answer(message):
                await ws.send(json.dumps(message, ensure_ascii=False))
            questions = QuestionClient(answer, ROOT / 'question.sock') if relay_questions else None
            initialize_id = None
            client_name = None
            def record_connection(connected):
                if not relay_questions:
                    return
                path = ROOT / 'ide-shared-connection.json'
                temporary = path.with_name('.' + path.name + '-' + str(os.getpid()))
                try:
                    temporary.write_text(json.dumps({
                        'updated_at': datetime.now().astimezone().isoformat(),
                        'proxy_pid': os.getpid(), 'backend_socket': socket,
                        'client_name': client_name, 'initialized': connected}, indent=2) + '\n')
                    temporary.chmod(0o600)
                    temporary.replace(path)
                except OSError:
                    temporary.unlink(missing_ok=True)
            async def upload():
                nonlocal initialize_id, client_name
                while line := await reader.readline():
                    message = json.loads(line)
                    if message.get('method') == 'initialize':
                        initialize_id = message.get('id')
                        client_name = message.get('params', {}).get('clientInfo', {}).get('name')
                    await ws.send(line.decode('utf-8').rstrip('\r\n'))

            async def download():
                async for frame in ws:
                    if not isinstance(frame, str):
                        raise RuntimeError('Shared app-server returned a non-text frame.')
                    # The original IDE connection forwards questions even when
                    # the Feishu bot is following a different thread.
                    message = json.loads(frame)
                    if initialize_id is not None and message.get('id') == initialize_id and 'result' in message:
                        record_connection(True)
                    forwarded = await questions.observe(message) if questions else False
                    if forwarded and not message['params'].get('isBlocking', True):
                        # VS Code auto-dismisses nonblocking questions after
                        # 90 seconds. A relayed question waits for the user's
                        # phone reply; change only the IDE presentation flag,
                        # leaving the server's asynchronous tool semantics intact.
                        message = dict(message, params=dict(message['params'], isBlocking=True))
                        frame = json.dumps(message, ensure_ascii=False)
                    await asyncio.to_thread(sys.stdout.buffer.write, (frame + '\n').encode('utf-8'))
                    await asyncio.to_thread(sys.stdout.buffer.flush)

            tasks = [asyncio.create_task(upload()), asyncio.create_task(download())]
            try:
                finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    with suppress(ConnectionClosedOK):
                        task.result()
            finally:
                record_connection(False)
                if questions:
                    await questions.close()
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError, ConnectionClosedOK):
                        await task
    finally:
        transport.close()


def main(arguments):
    os.umask(0o077)
    runtime = load_runtime()
    binary = str(real_binary(runtime))
    if len(arguments) == 2 and arguments[0] == '--proxy':
        if arguments[1] != runtime['socket']:
            raise RuntimeError('Proxy socket differs from the configured shared backend.')
        wait_for_backend(runtime)
        asyncio.run(relay_stdio(runtime['socket'], relay_questions=False))
        return
    if arguments == ['--serve']:
        env = dict(os.environ, CODEX_HOME=str(Path(runtime['codex_home']).expanduser().resolve()))
        os.execve(binary, [binary, '-c', 'features.code_mode_host=true', 'app-server',
            '--listen', 'unix://' + runtime['socket']], env)
    if is_stdio_app_server(arguments):
        wait_for_backend(runtime)
        # proxy is a separate client connection. Terminating it leaves the
        # independently managed backend and its running turns alive.
        asyncio.run(relay_stdio(runtime['socket']))
        return
    os.execv(binary, [binary, *arguments])


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except Exception as exc:
        print('Codex shared launcher: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)

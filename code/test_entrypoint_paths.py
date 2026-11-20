import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class EntrypointPathTests(unittest.TestCase):
    def test_entrypoints_use_runtime_at_project_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'project with spaces'
            code = project / 'code'
            shutil.copytree(Path(__file__).resolve().parent, code,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            runtime = project / 'runtime'
            runtime.mkdir()

            def private_file(name, content):
                path = runtime / name
                path.write_text(content, encoding='utf-8')
                path.chmod(0o600)
                return path

            private_file('config.json', '{"app_id": "", "app_secret": ""}')
            private_file('server-monitor.json', '{"enabled": false}')
            private_file('feishu-notify.json', '{"enabled": false, '
                         '"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/placeholder", '
                         '"signing_secret": "example-secret"}')
            fake_codex = project / 'fake-codex'
            fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME" "$@"\n', encoding='utf-8')
            fake_codex.chmod(0o700)
            codex_home = project / 'codex-home'
            socket = runtime / 'codex.sock'
            private_file('shared-runtime.json', json.dumps({
                'codex_binary': str(fake_codex), 'codex_home': str(codex_home),
                'socket': str(socket), 'manage_service': False,
            }))
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            for key in ('CODEX_SHARED_ROOT', 'CODEX_HOME', 'PYTHONPATH', 'PYTHONHOME'):
                env.pop(key, None)

            def run(script, *args):
                return subprocess.run([sys.executable, str(code / script), *args], cwd=project,
                                      env=env, capture_output=True, text=True, timeout=10)

            for script in ('bridge.py', 'sync_commands.py'):
                result = run(script)
                self.assertNotEqual(result.returncode, 0, script)
                self.assertIn('App credentials are missing.', result.stderr, script)
            for script, args in (('server_monitor.py', ()),
                                 ('notify_feishu.py', ('--message', 'offline test'))):
                result = run(script, *args)
                self.assertEqual(result.returncode, 0, (script, result.stderr))
            result = run('shared_backend.py', '--serve')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [str(codex_home), '-c',
                'features.code_mode_host=true', 'app-server', '--listen', 'unix://' + str(socket)])

    def test_shell_launcher_uses_project_venv_and_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / 'project with spaces'
            code = project / 'code'
            code.mkdir(parents=True)
            launcher = code / 'codex-shared'
            shutil.copy2(Path(__file__).with_name('codex-shared'), launcher)
            python = project / '.venv' / 'bin' / 'python'
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$CODEX_SHARED_ROOT" "$@"\n', encoding='utf-8')
            python.chmod(0o700)
            env = dict(os.environ)
            env.pop('CODEX_SHARED_ROOT', None)
            env.pop('CODEX_BRIDGE_PYTHON', None)
            result = subprocess.run([str(launcher), '--version'], cwd=project, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [str(project / 'runtime'),
                             str(code / 'shared_backend.py'), '--version'])
            env['CODEX_SHARED_ROOT'] = str(project / 'other-runtime')
            env['CODEX_BRIDGE_PYTHON'] = str(python)
            result = subprocess.run([str(launcher), '--version'], cwd=project, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[0], str(project / 'other-runtime'))


if __name__ == '__main__':
    unittest.main()

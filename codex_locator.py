"""Find the real Codex CLI without relying on a particular user's home path."""
import os
from pathlib import Path
import shutil


def find_codex(explicit=None):
    selected = explicit or os.environ.get('CODEX_BINARY')
    if selected:
        binary = Path(selected).expanduser()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError('Configured Codex executable is missing or not executable.')
        return binary.resolve()
    binary = shutil.which('codex')
    if binary:
        return Path(binary).resolve()
    home = Path.home()
    candidates = []
    for extensions in (home / '.vscode-server' / 'extensions', home / '.vscode' / 'extensions'):
        candidates.extend(path for path in extensions.glob('openai.chatgpt-*/bin/*/codex')
                          if path.is_file() and os.access(path, os.X_OK))
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime).resolve()
    raise RuntimeError('Codex executable not found; set codex_binary or CODEX_BINARY.')

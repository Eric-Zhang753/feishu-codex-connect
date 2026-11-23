"""Extract user-facing attachments from a completed Codex result."""
import base64
import binascii
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit


MAX_IMAGE_BYTES = 10_000_000
MAX_FILE_BYTES = 30_000_000
MAX_ATTACHMENTS = 6
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
FILE_TYPES = {
    '.pdf': ('pdf', 'application/pdf'),
    '.doc': ('doc', 'application/msword'),
    '.xls': ('xls', 'application/vnd.ms-excel'),
    '.ppt': ('ppt', 'application/vnd.ms-powerpoint'),
    '.docx': ('stream', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    '.xlsx': ('stream', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    '.pptx': ('stream', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    '.csv': ('stream', 'text/csv'),
}
FENCE = re.compile(r' {0,3}(`{3,}|~{3,})(.*)')
MARKUP = re.compile(
    r'(?P<ticks>`+)(?!`).*?(?<!`)(?P=ticks)(?!`)'
    r'|(?<!\\)(?P<image>!)?\[(?P<label>[^\]\n]*)\]\('
    r'(?:<(?P<angle>[^>\n]+)>|(?P<bare>(?:[^\s()\\]|\\[()]|\([^()\r\n]*\))+))'
    r'(?:[ \t]+(?:"[^"\n]*"|\'[^\'\n]*\'))?[ \t]*\)'
)


def local_attachment_path(target):
    """Interpret workspace links and Codex's sandbox export links as local paths."""
    try:
        url = urlsplit(re.sub(r'\\([()])', r'\1', target))
    except ValueError:
        return None
    if url.scheme not in ('', 'sandbox', 'file') or url.netloc:
        return None
    path = Path(unquote(url.path))
    if path.suffix.lower() in IMAGE_SUFFIXES or path.suffix.lower() in FILE_TYPES:
        return path
    return None


def extract_markdown_attachments(text):
    """Keep code examples literal and turn remote image markup into clickable links."""
    references = {}
    fence = ''
    lines = []

    def replace(match):
        if match['ticks'] is not None:
            return match[0]
        target = match['angle'] or match['bare']
        path = local_attachment_path(target)
        if path is not None:
            references.setdefault(path, None)
            return match['label'] or path.name
        if match['image'] and target.lower().startswith(('https://', 'http://')):
            return match[0][1:]
        return match[0]

    for line in text.splitlines(keepends=True):
        marker = FENCE.fullmatch(line.rstrip('\r\n'))
        if marker:
            sequence, suffix = marker.groups()
            if not fence:
                fence = sequence
            elif sequence[0] == fence[0] and len(sequence) >= len(fence) and not suffix.strip():
                fence = ''
            lines.append(line)
        else:
            lines.append(line if fence else MARKUP.sub(replace, line))
    return ''.join(lines), tuple(references)


def image_format(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png', 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg', 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'gif', 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    return None


def read_local_attachment(path, workspace, *, limit, generated_dir=None):
    try:
        path = (workspace / path).resolve(strict=True)
        roots = (workspace.resolve(), Path('/mnt/data'), generated_dir)
        if not any(root is not None and path.is_relative_to(root) and
                   all(not part.startswith('.') for part in path.relative_to(root).parts) for root in roots):
            return b''
        if not path.is_file():
            return b''
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                return b''
            # Confirm the opened file is still the path checked above.
            if Path(f'/proc/self/fd/{descriptor}').resolve(strict=True) != path:
                return b''
            data = stream.read(limit + 1)
            return data if len(data) <= limit else b''
    except (OSError, RuntimeError, ValueError):
        return b''


def remember_generated_image(turn, item):
    if item.get('type') == 'imageGeneration' and item.get('status') == 'completed' and item.get('id'):
        images = turn.setdefault('images', {})
        if len(images) < MAX_ATTACHMENTS:
            images.setdefault(item['id'], item)


def generated_image_bytes(item, workspace, generated_dir):
    target = item.get('savedPath')
    if isinstance(target, str):
        local = local_attachment_path(target)
        if local is not None and local.suffix.lower() in IMAGE_SUFFIXES:
            data = read_local_attachment(local, workspace, limit=MAX_IMAGE_BYTES, generated_dir=generated_dir)
            if image_format(data):
                return data
    encoded = item.get('result')
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        return b''
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return b''

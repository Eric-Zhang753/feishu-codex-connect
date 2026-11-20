#!/usr/bin/env python3
"""Send Codex turn-completion notifications to a signed Feishu webhook.

Usage: python code/notify_feishu.py '<Codex JSON payload>'
       python code/notify_feishu.py --test
       python code/notify_feishu.py --message 'Experiment finished; see its output directory.'
       python code/notify_feishu.py --dry-run '<Codex JSON payload>'

Credentials and the enabled switch live in sibling feishu-notify.json (mode 600).
Messages use Feishu Card JSON 2.0 Markdown by default; message_format="text"
in the configuration restores the original plain-text format.
Only the final assistant message is used; user prompts are never forwarded.
The complete reply is sent, using numbered messages when necessary. There is
no reply-length cap; the per-message budget only keeps each webhook request
within Feishu's transport limit.
"""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


class NotifyError(Exception):
    """An error whose message is safe to write to the notification log."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sign(timestamp, secret):
    key = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")


def load_config(path):
    if path.stat().st_mode & 0o077:
        raise NotifyError("credentials file must be readable only by its owner (chmod 600)")
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise NotifyError("credentials file must contain a JSON object")
    webhook, secret = config.get("webhook_url", ""), config.get("signing_secret", "")
    if not isinstance(webhook, str) or not re.fullmatch(
        r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[A-Za-z0-9-]+", webhook
    ):
        raise NotifyError("invalid Feishu webhook URL in credentials file")
    if not isinstance(secret, str) or not secret:
        raise NotifyError("missing Feishu signing secret")
    if config.get("message_format", "markdown") not in ("markdown", "text"):
        raise NotifyError("message_format must be markdown or text")
    return config


def redact(text, config):
    text = str(text)
    for value in (config["webhook_url"], config["webhook_url"].rsplit("/", 1)[-1],
                  config["signing_secret"]):
        text = text.replace(value, "[redacted]")
    text = re.sub(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[^\s)<>]+",
                  "[redacted webhook]", text)
    # A quoted assistant message must not accidentally mention group members.
    return re.sub(r"<at\b", "&lt;at", text, flags=re.IGNORECASE)


def normalize_text(text):
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def message_parts(event, config, kind):
    cwd = str(event.get("cwd") or os.getcwd())
    project = Path(cwd).name or "Codex"
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S %Z")
    title = {"test": "Codex 飞书通知测试", "manual": "Codex 任务通知",
             "turn": "Codex 本轮工作已结束"}[kind]
    summary = str(event.get("last-assistant-message") or "请回到 Codex 查看结果。")
    summary = normalize_text(redact(summary, config))
    return {"title": title, "project": normalize_text(redact(project, config))[:200],
            "time": now, "summary": summary,
            "cwd": normalize_text(redact(cwd, config))[:500],
            "thread": normalize_text(redact(event.get("thread-id") or "", config))[:80]}


def format_message(event, config, kind):
    parts = message_parts(event, config, kind)
    summary = parts["summary"]
    lines = [parts["title"], f"项目：{parts['project']}", f"时间：{parts['time']}",
             "", summary]
    return "\n".join(lines)


def markdown_literal(text):
    """Keep metadata literal while preserving the assistant's Markdown body."""
    text = " ".join(text.splitlines())
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>~<>])", r"\\\1", text)


def split_card_markdown(text, limit=3600):
    """Return (source, rendered) chunks without dropping any source text.

    The budget applies to each card, never to the whole reply. Unicode and
    JSON escaping use at most four bytes per normalized input character, so
    this leaves space for the card metadata in the 20 KB webhook request.
    Reopen code fences and repeat table headers on continuation cards.
    """
    if limit < 256:
        raise ValueError("Card chunk budget must be at least 256 characters.")
    lines = text.splitlines(keepends=True)
    chunks, buffer, source = [], "", ""
    fence, opener, table_header, prefix = "", "", "", ""
    fence_pattern = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)$")
    table_separator = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")

    def flush():
        nonlocal buffer, source, prefix
        if not source:
            return
        suffix = ("\n" if not buffer.endswith("\n") else "") + fence if fence else ""
        chunks.append((source, buffer + suffix))
        prefix = opener + "\n" if fence else table_header
        buffer, source = prefix, ""

    index = 0
    while index < len(lines):
        unit = lines[index]
        next_header = None
        if not fence:
            if table_header and ("|" not in unit or not unit.strip()):
                table_header = ""
            if ("|" in unit and index + 1 < len(lines) and
                    table_separator.fullmatch(lines[index + 1].rstrip("\n"))):
                unit += lines[index + 1]
                next_header = unit
                index += 1
        match = fence_pattern.fullmatch(unit.rstrip("\n")) if next_header is None else None
        next_fence, next_opener = fence, opener
        if match:
            marker, info = match.groups()
            if not fence:
                if len(unit) < limit // 4 and (marker[0] != "`" or "`" not in info):
                    next_fence, next_opener = marker, unit.rstrip("\n")
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not info.strip():
                next_fence, next_opener = "", ""
        remaining = unit
        while remaining:
            available = limit - len(buffer) - len(next_fence) - 2
            if len(remaining) <= available:
                buffer += remaining
                source += remaining
                remaining = ""
            elif source:
                flush()
            else:
                # A single very long line must still make progress.
                if available <= 0:
                    prefix, buffer, table_header = "", "", ""
                    available = limit - len(next_fence) - 2
                buffer += remaining[:available]
                source += remaining[:available]
                remaining = remaining[available:]
                flush()
        fence, opener = next_fence, next_opener
        if next_header is not None:
            table_header = next_header if len(next_header) < limit // 2 else ""
        index += 1
    flush()
    return chunks or [("", "（无正文）")]


def format_card(parts, content, index=1, total=1):
    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": parts["title"] +
                                 (f" · {index}/{total}" if total > 1 else "")},
                       "template": "blue"},
            "body": {"elements": [
                {"tag": "markdown", "content":
                 f"**项目：** {markdown_literal(parts['project'])}\n**时间：** {parts['time']}"},
                {"tag": "markdown", "content": content}]}}


def format_notifications(event, config, kind):
    if config.get("message_format", "markdown") == "text":
        text = format_message(event, config, kind)
        chunks = [text[start:start + 3600] for start in range(0, len(text), 3600)]
        return [{"msg_type": "text", "content": {"text":
                 (f"Codex 通知 · {index}/{len(chunks)}\n\n" if len(chunks) > 1 else "") + chunk}}
                for index, chunk in enumerate(chunks, 1)]
    parts = message_parts(event, config, kind)
    chunks = split_card_markdown(parts["summary"])
    return [{"msg_type": "interactive", "card": format_card(parts, rendered, index, len(chunks))}
            for index, (_, rendered) in enumerate(chunks, 1)]


def send(config, message):
    opener = build_opener(NoRedirect())
    for attempt in range(2):
        timestamp = str(int(time.time()))
        payload = dict(message, timestamp=timestamp, sign=sign(timestamp, config["signing_secret"]))
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 20000:
            raise NotifyError("notification exceeds the Feishu webhook 20 KB request limit")
        request = Request(config["webhook_url"],
                          data=encoded,
                          headers={"Content-Type": "application/json; charset=utf-8"},
                          method="POST")
        retry = False
        try:
            with opener.open(request, timeout=6) as response:
                result = json.loads(response.read(65536))
            if not isinstance(result, dict):
                raise NotifyError("Feishu returned an invalid response")
            code = result.get("code", result.get("StatusCode"))
            if code == 0:
                return
            if not isinstance(code, int):
                raise NotifyError("Feishu response did not confirm success")
            hints = {19021: "signature or timestamp rejected", 19022: "IP not allowed",
                     19024: "required keyword missing", 11232: "rate limited"}
            error = f"Feishu rejected notification (code={code}, {hints.get(code, 'API error')})"
            retry = code == 11232
        except HTTPError as exc:
            error = f"Feishu HTTP error {exc.code}"
            retry = exc.code == 429 or exc.code >= 500
            exc.close()
        except (URLError, TimeoutError, OSError):
            error, retry = "Feishu network request failed", True
        except (ValueError, UnicodeError):
            error = "Feishu returned an invalid JSON response"
        if not retry or attempt == 1:
            raise NotifyError(error)
        time.sleep(1)


@contextmanager
def locked_state(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / "lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def event_key(event):
    if not event.get("thread-id") or not event.get("turn-id"):
        return None
    value = json.dumps([event["thread-id"], event["turn-id"]], ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_mapping(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_mapping(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def deliver(event, config, state_dir, kind):
    key = event_key(event) if kind == "turn" else None
    with locked_state(state_dir):
        sent_path = state_dir / "sent.json"
        sent = read_mapping(sent_path)
        if key and key in sent:
            return "duplicate"
        messages = format_notifications(event, config, kind)
        # Save only hashes and part numbers, never the assistant's reply.
        pending_path = state_dir / "pending.json"
        cutoff = time.time() - 7 * 86400
        pending = {k: v for k, v in read_mapping(pending_path).items()
                   if isinstance(v, dict) and isinstance(v.get("updated_at"), (int, float))
                   and v["updated_at"] > cutoff}
        stable = {k: v for k, v in message_parts(event, config, kind).items() if k != "time"}
        stable["message_format"] = config.get("message_format", "markdown")
        fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = pending.get(key, {})
        start = previous.get("next_part", 0) if previous.get("fingerprint") == fingerprint else 0
        if not isinstance(start, int) or not 0 <= start <= len(messages):
            start = 0
        for index in range(start, len(messages)):
            if index > start:
                # Stay below both the 5/second and 100/minute webhook limits.
                time.sleep(0.65)
            send(config, messages[index])
            if key:
                pending[key] = {"fingerprint": fingerprint, "next_part": index + 1,
                                "total_parts": len(messages), "updated_at": time.time()}
                pending = dict(list(pending.items())[-1000:])
                save_mapping(pending_path, pending)
        if key:
            sent = {k: v for k, v in sent.items() if isinstance(v, (int, float)) and v > cutoff}
            sent[key] = time.time()
            sent = dict(list(sent.items())[-1000:])
            save_mapping(sent_path, sent)
            pending.pop(key, None)
            save_mapping(pending_path, pending)
        with (state_dir / "events.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                                  "status": "sent", "kind": kind, "event_hash": key,
                                  "parts": len(messages)}) + "\n")
    return "sent"


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", nargs="?", help="Codex notification JSON")
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).resolve().parent.parent / "runtime" / "feishu-notify.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--test", action="store_true", help="send one connectivity test")
    modes.add_argument("--message", help="send an explicit task notification")
    parser.add_argument("--dry-run", action="store_true", help="preview redacted message JSON without sending")
    args = parser.parse_args(argv)
    state_dir = args.config.resolve().parent / "notify-feishu-state"
    try:
        if args.test or args.message is not None:
            if args.payload:
                raise NotifyError("a manual notification cannot also contain a Codex payload")
            kind = "test" if args.test else "manual"
            event = {"cwd": os.getcwd(), "last-assistant-message":
                     "签名验证及消息推送连通性测试。收到此消息表示飞书通知通道已连通。"
                     if args.test else args.message}
        else:
            if args.payload is None:
                parser.error("provide a Codex payload, --test, or --message")
            event = json.loads(args.payload)
            if not isinstance(event, dict):
                raise NotifyError("Codex payload must contain a JSON object")
            if event.get("type") != "agent-turn-complete":
                return 0
            kind = "turn"
        config = load_config(args.config)
        if not config.get("enabled", True):
            return 0
        if args.dry_run:
            messages = format_notifications(event, config, kind)
            print(json.dumps({"part_count": len(messages), "messages": messages},
                             ensure_ascii=False, indent=2))
            return 0
        status = deliver(event, config, state_dir, kind)
        if kind != "turn":
            print(f"Feishu notification: {status} (code=0)")
        return 0
    except Exception as exc:
        # Raw HTTP exception messages may contain the credential-bearing URL.
        detail = str(exc) if isinstance(exc, NotifyError) else type(exc).__name__
        print(f"Feishu notification failed: {detail}", file=sys.stderr)
        try:
            with locked_state(state_dir):
                with (state_dir / "events.jsonl").open("a", encoding="utf-8") as log:
                    log.write(json.dumps({"time": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                                          "status": "failed", "error": detail}) + "\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

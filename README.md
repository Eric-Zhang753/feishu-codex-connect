# Feishu ↔ Codex Connect

<img src="assets/logo-240.png" alt="Feishu ↔ Codex Connect logo" width="96" height="96">

English | [简体中文](README.zh-CN.md)

A bidirectional bridge between a Feishu bot and Codex on a Linux host. Authorized users can start tasks, inspect results, switch sessions, answer questions, and respond to approvals from a Feishu direct chat. This repository contains the bridge, an optional shared Codex backend, configuration templates, and a deployment guide.

Start with the [deployment guide for AI assistants](docs/AI_TUTORIAL.md) (currently in Chinese). Give the project directory to Codex on the target host and ask it to follow the guide, including the Feishu configuration and end-to-end checks that require your participation.

```text
Feishu direct chat ── bot long connection ──> code/bridge.py ── JSON-RPC ──> Codex app-server
                   <────── message cards ──────                         <── tasks and sessions

Optional: VS Code ── code/codex-shared proxy ──> the same local shared app-server
```

## Features and scope

- Text tasks run sequentially. SQLite stores deduplication records, session bindings, and the queue. Replies use Feishu Markdown cards, with long replies split across cards and message content truncated beyond 28,000 characters.
- Use `/sessions`, `/use`, `/current`, and `/new` to manage sessions; `/status`, `/stop`, and `/steer` to control tasks; `/models`, `/model`, and `/effort` to select models; and `/help` for available commands.
- Respond to Codex approvals and questions using `/approve`, `/deny`, `/answer`, or question cards. Some commands depend on support in the installed Codex version.
- An optional shared backend lets Feishu join a VS Code session on the same Codex backend. Other optional features include discovering questions from local sessions, server-monitoring cards, and standalone webhook notifications. See the deployment guide for their prerequisites.
- The project currently targets **Linux, Feishu China custom enterprise apps, and Python 3.11**, and accepts text messages only. The host needs outbound access to Feishu and Codex; no public inbound address is required. Shared-backend and IDE integrations depend on the Codex version and should be verified after upgrades.

**Access:** Authorized Feishu users can ask Codex to work in the host workspace. Session switching may also open other sessions under the same `CODEX_HOME`. Use a trusted direct chat, review approvals, and protect the host, bot credentials, and `runtime/` directory.

## Quick start

From the project root, install the locked dependencies using Python 3.11:

```bash
python -m pip install -r requirements.lock
mkdir -p runtime
chmod 700 runtime
cp examples/config.example.json runtime/config.json
chmod 600 runtime/config.json
```

Fill in the app credentials and an absolute workspace path in `runtime/config.json` using a local editor, then run:

```bash
python code/bridge.py --config runtime/config.json
```

Follow the [deployment guide](docs/AI_TUTORIAL.md) for the correct order of Feishu setup, direct-chat pairing, background services, and optional VS Code sharing. Do not share the live `runtime/` directory, databases, logs, session records, or credentials; the project’s `.gitignore` excludes the runtime directory.

If upgrading an existing installation, update the script paths in systemd services, the IDE launcher, and notification commands to their new locations under `code/`. Runtime data remains in the project-root `runtime/` directory.

Run the offline tests with:

```bash
python -m unittest discover -s code -v
```

## Files

- `code/bridge.py`: Feishu connection, authorization, task queue, and Codex app-server client.
- `code/shared_backend.py`, `code/codex-shared`, and `code/question_channel.py`: optional shared backend and IDE question relay.
- `code/global_questions.py` and `code/ide_ipc.py`: optional cross-session question discovery and IDE IPC.
- `code/goal_commands.py` and `code/message_feedback.py`: goal commands and message-reaction feedback.
- `code/sync_commands.py`, `code/server_monitor.py`, and `code/notify_feishu.py`: optional command panel, server-monitoring cards, and one-way webhook notifications.
- `examples/`: configuration templates without real credentials; `code/test_*.py`: offline tests.

See the [architecture notes](docs/ARCHITECTURE.md) (Chinese) for design and operational constraints.

## License

The project code and documentation are available under the [MIT License](LICENSE). Third-party dependencies are subject to their own licenses.

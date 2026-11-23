# Feishu ↔ Codex Connect

<img src="assets/logo-240.png" alt="Feishu ↔ Codex Connect logo" width="96" height="96">

English | [简体中文](README.zh-CN.md)

Use Codex on your Linux host from a Feishu chat on your phone or desktop.

## Highlights

- **Set up by QR code.** Scan with the Feishu app to create a bot or add the required permissions to an existing one. Follow Feishu’s prompts for publishing and any approvals required by your organization.
- **Control tasks from your phone.** Send a task in a direct chat, check progress with `/status`, give Codex more direction with `/steer`, or stop a run with `/stop`. You can also switch models and adjust reasoning effort from the chat.
- **Continue a VS Code session in Feishu.** With the optional shared backend, use `/sessions` and `/use` to select a session you started in VS Code and continue the conversation on either client.
- **Answer questions and review approvals in chat.** Pick an option on a question card or reply with `/answer`. Use `/approve` and `/deny` to handle Codex approval requests. You can also relay questions from other local sessions to your direct chat.
- **Track results and host health.** Read Markdown cards, view generated images, and download files such as PDFs from direct chat. Follow task status through optional message reactions, check host metrics in a monitoring card, or receive completion notifications through a webhook.

Give this project directory to Codex on the target host and ask it to follow the deployment guide. [Deployment guide for AI assistants (Chinese)](docs/AI_TUTORIAL.md)

> Please read `docs/AI_TUTORIAL.md` and set up two-way Feishu–Codex messaging on this machine, starting with direct chat and adding VS Code session sharing if I need it.

```text
Feishu direct chat ── bot long connection ──> code/bridge.py ── JSON-RPC ──> Codex app-server
                   <────── message cards ──────                         <── tasks and sessions

Optional: VS Code ── code/codex-shared proxy ──> the same local shared app-server
```

## Requirements and access

The bridge supports **Linux, Feishu China custom enterprise apps, and Python 3.11**. The host connects to Feishu and Codex over outbound connections. It accepts text input, runs tasks sequentially, and stores message deduplication records, session bindings, and the queue in SQLite. Long replies span multiple cards and are capped at 28,000 characters. Shared sessions, IDE question relays, and some commands depend on the installed Codex version.

The bridge grants access through a Feishu user allowlist or a one-time pairing code. Authorized users can ask Codex to work in the host workspace and may open other sessions under the same `CODEX_HOME`. Use a trusted direct chat, review approvals, and protect the host, bot credentials, and `runtime/` directory.

## Quick start

From the project root, install the locked dependencies using Python 3.11 and set up a new bot by QR code.

```bash
python -m pip install -r requirements.lock
python code/setup_feishu.py --project-dir /absolute/path/to/workspace
python code/bridge.py --config runtime/config.json
```

Scan once with the Feishu mobile app to request direct messages, group mentions, image and file uploads, card callbacks, reactions, slash commands, and monitoring-card pinning. Follow Feishu’s prompts to publish the app or submit it for approval. To add the same permissions to an existing bot, run `python code/setup_feishu.py --update-permissions --config /path/to/config.json`.

Follow the [deployment guide](docs/AI_TUTORIAL.md) for Feishu release and approval, verifying direct chat and card buttons, manual setup, background services, and optional VS Code sharing. Keep the live `runtime/` directory, databases, logs, session records, and credentials private. The project’s `.gitignore` excludes the runtime directory.

If upgrading an existing installation, update the script paths in systemd services, the IDE launcher, and notification commands to their new locations under `code/`. Runtime data remains in the project-root `runtime/` directory.

Run the offline tests with:

```bash
python -m unittest discover -s code -v
```

## Files

- `code/bridge.py`: Feishu connection, authorization, task queue, and Codex app-server client.
- `code/setup_feishu.py` creates a bot or adds permissions to an existing bot using Feishu QR authorization.
- `code/result_attachments.py` reads generated images and linked local files for delivery in direct chat.
- `code/shared_backend.py`, `code/codex-shared`, and `code/question_channel.py`: optional shared backend and IDE question relay.
- `code/global_questions.py` and `code/ide_ipc.py`: optional cross-session question discovery and IDE IPC.
- `code/goal_commands.py` and `code/message_feedback.py`: goal commands and message-reaction feedback.
- `code/sync_commands.py`, `code/server_monitor.py`, and `code/notify_feishu.py`: optional command panel, server-monitoring cards, and one-way webhook notifications.
- `examples/`: configuration templates without real credentials; `code/test_*.py`: offline tests.

See the [architecture notes](docs/ARCHITECTURE.md) (Chinese) for design and operational constraints.

## License

The project code and documentation are available under the [MIT License](LICENSE). Third-party dependencies are subject to their own licenses.

# Feishu ↔ Codex Connect

<img src="assets/logo-240.png" alt="Feishu ↔ Codex Connect 项目标志" width="96" height="96">

[English](README.md) | 简体中文

在手机或电脑的飞书里，直接使用运行在 Linux 主机上的 Codex。

## 功能亮点

- **一次扫码接入飞书。** 用手机飞书扫码创建机器人，也可以给已有机器人一次补齐桥接权限。发布和审批按飞书所在组织的规则执行。
- **在手机上控制 Codex 任务。** 私聊发一条消息就能提交任务，用 `/status` 看进度、`/steer` 补充要求、`/stop` 中止任务，也可以在聊天里切换模型或调整推理强度。
- **在飞书继续 VS Code 的会话。** 配置共享后端后，用 `/sessions` 和 `/use` 找到在 VS Code 开始的会话，从手机或 IDE 继续同一段对话。
- **在聊天里回答问题、处理审批。** 可以点问题卡片上的选项或用 `/answer` 回复，用 `/approve` 或 `/deny` 处理 Codex 的审批请求。也可将其他本机会话中的待答问题转到飞书私聊。
- **在飞书查看结果和主机状态。** 任务结果通过 Markdown 卡片返回，可启用消息表情追踪任务状态。独立监控卡片可展示 CPU、内存和可用的 GPU 指标，也可配置 webhook 接收任务完成通知。

把项目目录交给目标主机上的 Codex，让它按教程协助部署。 [给 AI 的部署教程](docs/AI_TUTORIAL.md)

> 请阅读 `docs/AI_TUTORIAL.md`，帮我在这台机器上部署飞书与 Codex 的双向通信。先完成私聊功能，再根据我的需要配置 VS Code 共享会话。

```text
飞书私聊 ──应用机器人长连接──> code/bridge.py ──JSON-RPC──> Codex app-server
       <────── 消息卡片 ──────┘                      └─ 任务和会话

可选：VS Code ──code/codex-shared 代理──> 同一个本地共享 app-server
```

## 运行环境与访问权限

项目面向 **Linux、飞书中国站企业自建应用和 Python 3.11**。主机主动连接飞书和 Codex 服务。桥接接收文本消息并顺序执行任务，用 SQLite 管理消息去重、会话绑定和任务队列。长回复会拆分成多张卡片，正文上限为 28,000 字符。共享会话、IDE 问题回传和部分命令依赖所用的 Codex 版本。

桥接通过飞书用户白名单或一次性配对码授权。被授权的账号可以让 Codex 操作主机工作区，也可能打开同一 `CODEX_HOME` 下的其他会话。请在可信私聊中使用，审阅审批内容，并保护好本机、机器人凭据和 `runtime/`。

## 本地启动概览

在项目目录中，使用 Python 3.11 环境安装依赖并扫码创建机器人。

```bash
python -m pip install -r requirements.lock
python code/setup_feishu.py --project-dir /absolute/path/to/workspace
python code/bridge.py --config runtime/config.json
```

用手机飞书扫码并确认，可一次申请私聊收发、卡片按钮回调、表情反馈和原生命令面板所需的权限。加上 `--group --server-monitor` 可同时申请群聊 @机器人和监控卡片置顶权限。已有机器人可用 `python code/setup_feishu.py --update-permissions --config /path/to/config.json` 扫码补权。

发布审批、私聊验收、手动接入、后台服务和共享 VS Code 的操作见 [部署教程](docs/AI_TUTORIAL.md)。请妥善保管 `runtime/`、数据库、日志、会话记录和凭据。`.gitignore` 已忽略本项目运行目录。

从旧版本升级时，请将现有 systemd 服务、IDE 启动器和通知命令中的脚本路径更新为 `code/` 下的新路径；运行数据仍放在项目根目录的 `runtime/` 中。

离线测试使用 `python -m unittest discover -s code -v`。

## 文件

- `code/bridge.py`：飞书长连接、授权、任务队列和 Codex app-server 客户端。
- `code/setup_feishu.py` 用于扫码创建机器人和给已有机器人补权。
- `code/shared_backend.py`、`code/codex-shared`、`code/question_channel.py`：可选的本地共享后端及 IDE 问题回传。
- `code/global_questions.py`、`code/ide_ipc.py`：可选的跨会话问题发现与 IDE IPC 回传。
- `code/goal_commands.py`、`code/message_feedback.py`：目标命令和消息表情反馈。
- `code/sync_commands.py`、`code/server_monitor.py`、`code/notify_feishu.py`：可选的命令面板、服务器监控卡片与单向 webhook 通知。
- `examples/`：不含真实凭据的配置模板；`code/test_*.py`：迁移并补充的离线测试。

更多设计和操作约束见 [机制说明](docs/ARCHITECTURE.md)。

## 许可证

本项目的代码和文档采用 [MIT License](LICENSE)。第三方依赖遵循各自的许可证。

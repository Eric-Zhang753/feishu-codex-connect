# Feishu ↔ Codex Connect

把飞书应用机器人连接到一台 Linux 主机上的 Codex，让授权用户在飞书私聊中发起任务、查看结果、切换会话、回答问题和处理审批。本项目整理自一套实际运行的飞书桥接及 Codex 共享后端；部署代码、示例配置和面向 AI 的操作教程都在本目录内。

从 [给 AI 的部署教程](docs/AI_TUTORIAL.md) 开始：把这个项目目录交给目标主机上的 Codex，要求它阅读教程并协助部署。需要用户参与的飞书后台操作、凭据录入和端到端验证，教程也有对应步骤。

可以直接对目标主机上的 Codex 说：

> 请阅读 `docs/AI_TUTORIAL.md`，帮我在这台机器上部署飞书与 Codex 的双向通信。先完成私聊功能，再根据我的需要配置 VS Code 共享会话。

```text
飞书私聊 ──应用机器人长连接──> bridge.py ──JSON-RPC──> Codex app-server
       <────── 消息卡片 ──────┘                      └─ 任务和会话

可选：VS Code ──codex-shared 代理──> 同一个本地共享 app-server
```

## 功能与边界

- 文本任务顺序执行；SQLite 记录去重、会话绑定和队列。回复采用飞书 Markdown 卡片，过长内容分卡展示，正文超过 28,000 字符会截断。
- `/sessions`、`/use`、`/current`、`/new` 切换会话；`/status`、`/stop`、`/steer` 控制任务；`/models`、`/model`、`/effort` 调整模型；`/help` 查看命令。
- 支持 Codex 审批和问题：通过 `/approve`、`/deny`、`/answer` 或问题卡片回答。目标模式命令依赖所用 Codex 版本支持相应接口。
- 可选共享后端让飞书加入同一 Codex 后端中的 VS Code 会话；可选扫描本机其他会话的问题；可选服务器状态卡片和独立 webhook 通知。各功能的启用条件见教程。
- 目前针对 **Linux、飞书中国站企业自建应用和 Python 3.11**；只接收文本消息。长连接需要主机可访问飞书和 Codex 服务，无需公网入站地址。共享后端、IDE IPC、部分 Codex 接口依赖版本，升级后需要重新验证。

**使用权限**：被授权的飞书账号可以让 Codex 操作主机工作区；会话切换还可能打开同一 `CODEX_HOME` 下的其他会话。先在可信私聊中使用，审阅审批内容，并保护好本机、机器人凭据和 `runtime/`。

## 本地启动概览

在项目目录中，使用 Python 3.11 环境安装锁定依赖：

```bash
python -m pip install -r requirements.lock
mkdir -p runtime
chmod 700 runtime
cp examples/config.example.json runtime/config.json
chmod 600 runtime/config.json
```

用本地编辑器填写 `runtime/config.json` 的应用凭据和绝对工作区路径，之后执行：

```bash
python bridge.py --config runtime/config.json
```

首次启动、飞书订阅、私聊配对、后台服务和共享 VS Code 连接的先后顺序请遵循 [部署教程](docs/AI_TUTORIAL.md)。不要把运行中的 `runtime/`、数据库、日志、会话记录或凭据打包分享；`.gitignore` 已忽略本项目运行目录。

## 文件

- `bridge.py`：飞书长连接、授权、任务队列和 Codex app-server 客户端。
- `shared_backend.py`、`codex-shared`、`question_channel.py`：可选的本地共享后端及 IDE 问题回传。
- `global_questions.py`、`ide_ipc.py`：可选的跨会话问题发现与 IDE IPC 回传。
- `goal_commands.py`、`message_feedback.py`：目标命令和消息表情反馈。
- `sync_commands.py`、`server_monitor.py`、`notify_feishu.py`：可选的命令面板、服务器监控卡片与单向 webhook 通知。
- `examples/`：不含真实凭据的配置模板；`test_*.py`：迁移并补充的离线测试。

更多设计和操作约束见 [机制说明](docs/ARCHITECTURE.md)。

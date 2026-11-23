# 给 Codex 的部署教程

## 任务与工作方式

你正在帮助用户把本项目部署到用户自己的 Linux 主机。先读取本文件、项目 README 和相关源码，检查用户选择的工作区、Python、Codex 登录状态和飞书应用，再逐项配置并验证。默认先实现**飞书私聊双向交互**，只有用户需要时再配置 VS Code 共享会话、全局问题、监控卡片或 webhook 通知。

不要读取或复制其他安装中的真实配置、数据库和日志作为模板。不要把 App Secret、token、webhook、绑定码或用户会话内容写入对话、命令行参数、Git 或示例文件。让用户在目标主机的私有终端或编辑器中录入凭据；如果无法访问飞书管理后台，给出当前需要用户完成的具体操作，然后继续可独立完成的本地工作。应用发布、管理员审批或用户私聊验证需要实际结果，不要用模拟测试冒充成功。

除非另有说明，以下命令在项目根目录执行。从旧版本升级时，请将已有 systemd 服务、IDE 启动器和通知命令中的脚本路径更新到 `code/`；运行数据仍位于项目根目录的 `runtime/`。

## 1. 确认环境

1. 确认目标系统为 Linux，项目源码目录可读，准备好绝对路径形式的工作区；此目录是新建飞书会话的初始 `cwd`。`/sessions` 和 `/use` 可以切换到同一 Codex 数据目录的其他工作区会话，不能把初始 `cwd` 当作唯一安全边界。
2. 选 Python 3.11，创建项目内 `.venv` 或复用用户指定环境，然后运行 `python -m pip install -r requirements.lock`。后续命令中的 `python` 必须指向同一环境。若使用 `.venv`，`code/codex-shared` 会自动使用它；使用其他环境时，IDE 启动器必须能继承 `CODEX_BRIDGE_PYTHON`，或生成一个指向该 Python 的本地包装脚本。
3. 找到**真实** Codex 可执行文件并记录绝对路径，用 `codex --version`、`codex app-server --help` 检查版本及参数，确认用户已在将运行服务的同一 Linux 用户、同一 `CODEX_HOME` 中完成 Codex 登录。不要把本项目的 `code/codex-shared` 指定为真实可执行文件，以免递归。
4. 该实现使用 `app-server` 的 `initialize`、线程和回合等接口；共享模式还使用 `unix://` 监听，以及某些实验性接口。必要时参考 [OpenAI app-server 实现](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md) 和所安装版本的 `--help`。记录实际版本；版本改变后重新做验收。

## 2. 扫码申请飞书权限

新部署在用户的私有终端执行以下命令，用手机中国版飞书扫码并确认。使用第二条命令启动桥接。

```bash
python code/setup_feishu.py --project-dir /absolute/path/to/workspace
python code/bridge.py --config runtime/config.json
```

已有机器人使用现有的私有配置文件指定目标应用，通过同一次扫码增量申请权限。

```bash
python code/setup_feishu.py --update-permissions --config /path/to/config.json
```

- 新建和补权默认使用同一套扫码申请。一次申请私聊收发、群聊 @机器人、图片和文件上传（`im:resource`）、表情反馈、命令面板读写和监控卡片置顶，共八项应用身份权限，同时订阅 `im.message.receive_v1` 和 `card.action.trigger`。普通文本命令、审批、跨会话问题和 VS Code 共享复用桥接的消息权限。 [飞书扫码注册](https://pypi.org/project/lark-oapi/1.7.5/#one-click-app-registration) · [消息权限](https://open.feishu.cn/document/develop-robots/add-bot-to-external-group?lang=zh-CN) · [命令面板权限](https://open.larkenterprise.com/document/mcp_open_tools/agent-best-practices/agent-supports-slash-commands)
- 群聊和监控仍按使用场景配置。新建时加 `--group` 可预填群聊所需的 `bot_open_id`。命令面板条目由 `code/sync_commands.py --apply` 同步，监控服务和独立群 webhook 通知按第 6 节配置。需要根据已启用的功能控制申请范围时，可使用 `--configured-permissions`，并用 `--group` 或 `--server-monitor` 明确纳入相应权限。
- 新部署会把凭据保存到权限为 600 的配置文件。飞书返回可用的扫码账号 ID 时自动加入白名单，否则按第 3 节配对。为已有应用补权时继续使用现有配置和绑定。
- 飞书组织策略决定权限和版本是否免审。按扫码页提示完成发布或审批。桥接启动后用 `/help` 验证私聊，并点一次问题卡片按钮验证回调。若其中一项失败，到应用后台确认对应的事件或回调已使用**长连接**并发布。 [飞书自建应用审核规则](https://open.feishu.cn/document/best-practices/intro-to-custom-app-review?lang=zh-CN)

### 手动接入

1. 在[飞书开放平台](https://open.feishu.cn/app)创建**企业自建应用**，开启机器人能力。使用飞书中国站，不是 Lark 国际站或群自定义 webhook 机器人。长连接接收方式见[飞书官方说明](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)。
2. 私聊收发需要 `im:message.p2p_msg:readonly` 和 `im:message:send_as_bot`。发送图片和文件还需申请「获取与上传图片或文件资源」（`im:resource`）。将使用者加入应用可用范围，发布应用版本，并按飞书组织规则完成审批。 [飞书机器人示例](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/quick-start)
3. 在项目目录创建私有运行目录：`mkdir -p runtime && chmod 700 runtime`。复制 `examples/config.example.json` 到 `runtime/config.json` 并 `chmod 600 runtime/config.json`。让用户在本机安全编辑器填写 `app_id`、`app_secret`、已有的绝对 `project_dir`；`allowed_user_ids` 保持空数组以启用首次私聊配对。
4. 默认示例关闭消息表情反馈和跨会话问题扫描。可选添加 `codex_binary` 指向真实可执行文件、`codex_home` 指向正确的 Codex 数据目录；不设置时分别从 `CODEX_BINARY`/PATH 或默认 `~/.codex` 查找。`developer_instructions` 可选，用于新建会话，不必硬编码用户本机的虚拟环境。
5. 在私有终端前台启动 `python code/bridge.py --config runtime/config.json`。程序会先验证飞书凭据、连接 Codex，再建立飞书 SDK 长连接，并写入 `runtime/status.json` 和 `runtime/service.log`。只报告状态及错误类型，不展示文件中的敏感内容。
6. 在飞书应用的「事件与回调」配置里，选择**使用长连接接收事件**并订阅 `im.message.receive_v1`。如平台要求先存在连接，保持上一步程序运行再保存。若希望问题卡片按钮可点击，再为**回调**单独选择长连接并添加 `card.action.trigger`；不配置按钮时仍可用 `/answer` 文本回答。配置变更后按平台提示发布新版本。

## 3. 首次配对和真实验收

1. 未配置 `allowed_user_ids` 时，首次启动生成权限为 600 的 `runtime/pairing.json`，绑定码有效 24 小时。让用户在自己的私有终端查看该文件，并在飞书**与机器人私聊**中发送 `/bind <绑定码>`。不要让用户把绑定码交给 AI；绑定成功后文件会删除，绑定信息存在 `runtime/state.sqlite3`。绑定码过期时重启桥接生成新码。
2. 配对后，发送 `/help`、`/status` 和一个无副作用的简单任务，并核实收到回复和模型结果。第一次新任务会建立 Codex 会话。让 Codex 分别回复一张图片和一份 PDF，确认手机飞书能打开图片并下载文件。
3. 核实非授权用户不能提交任务；若已配置卡片回调，实际触发一次非敏感问题并测试按钮或 `/answer`。如需验证审批，用一个明确需要审批的低风险操作，确认用户回复后才执行。不同 Codex 版本可能新增审批类型或客户端能力要求；不支持的审批不能假定可以从飞书处理。
4. 离线测试可用 `python -m unittest discover -s code -v`；它不能替代真实飞书、Codex 登录、事件订阅和按钮回调的验收。`python code/bridge.py --config runtime/config.json --check-codex` 会向模型发送一次真实连通性请求，独立于共享后端，可能产生模型用量。

## 4. 配置后台运行

先检查系统是否有 user systemd、现有服务和同名安装。使用**当前项目的绝对路径**、当前 Python 和实际工作区生成用户级 unit，例如桥接 unit 的核心设置：

```ini
[Unit]
Description=Feishu Codex bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/feishu-codex-connect
ExecStart=/absolute/path/to/python /absolute/path/to/feishu-codex-connect/code/bridge.py --config /absolute/path/to/feishu-codex-connect/runtime/config.json
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=default.target
```

这是模板，不能直接安装；路径含空格或特殊字符时按 systemd 语法正确转义，所需 PATH/CODEX_HOME 等环境要明确配置，且不要把密钥放在 unit 文件里。放到 `~/.config/systemd/user/feishu-codex-bridge.service` 前先核对不会覆盖用户已有服务。`systemctl --user daemon-reload` 后启动并查看 unit 状态、私有日志和 `runtime/status.json`；通过验收后再启用自启动。用户希望退出登录后仍运行时，再依据主机环境确认 user manager 的 linger 设置。前台进程需先退出，避免同一应用启动两个消费者。

飞书长连接同一应用有多个客户端时按集群模式分发事件，并非每个实例都收到一份；不要拿同一个应用同时做新旧部署联调。不同安装要使用不同的私有 `runtime/`，套接字也会随运行目录隔离。

## 5. 可选：与 VS Code 共享同一个后端

此部分会改变用户的 IDE 启动路径，先确认用户确实要共享会话并查看当前 IDE 设置；不要擅自覆盖已有配置或打断正在运行的任务。先备份并记录变更，以便撤销。确保 Python 环境有 `websockets`，真实 Codex 支持 `app-server --listen unix://...`，后端和 IDE 使用同一 `CODEX_HOME`、同一用户和兼容版本。

1. 复制 `examples/shared-runtime.example.json` 为 `runtime/shared-runtime.json`，设置权限 600。填写 `codex_binary`、`codex_home`、`socket` 的真实绝对路径；套接字建议放在权限 700 的 `runtime/` 下，路径过长时改用较短的私有目录。`service` 填实际的 user systemd unit 名称；如果自行启动后端而不使用 systemd，设 `manage_service` 为 `false`。
2. 在 `runtime/config.json` 中添加 `"shared_socket"`，其值必须与 `shared-runtime.json` 的 `socket` **完全一致**；若显式设置 `codex_home`，也必须相同。桥接会将配置所在目录传给代理。`code/codex-shared` 默认使用本项目 `runtime/`；定制目录时需在 IDE 进程设置一致的 `CODEX_SHARED_ROOT`。
3. 用和第 4 节相同的方法准备独立后端 unit。它的 `ExecStart` 应为 `<绝对 Python> <项目>/code/shared_backend.py --serve`，设置 `Environment=CODEX_SHARED_ROOT=<项目>/runtime` 和适合真实 Codex 的 PATH，`UMask=0077`。先启动后端，再重启桥接。`manage_service=true` 时代理会尝试 `systemctl --user start` 此 unit；为 false 时用户必须预先启动它。
4. 确认当前 Codex VS Code 扩展实际提供 `chatgpt.cliExecutable` 设置且允许在目标主机的设置作用域生效；该设置可能被标记为开发用途或在升级后改变。将其设为本项目 `code/codex-shared` 的**绝对路径**，确保脚本可执行及 Python 可用，然后在任务空闲时重载扩展/窗口。若运行在远程主机，要检查远程机器的设置作用域；不要修改扩展安装文件来强制适配。设置无法生效时保留独立飞书模式并说明限制。
5. 在 IDE 新建或打开会话，飞书发送 `/sessions` 和 `/use <编号或完整会话ID>` 选择它，再分别从 IDE 和飞书做无副作用的轮次；验证对端能够看到同一会话的结果和控制。检查 `runtime/ide-shared-connection.json` 的 `initialized`，但只有真实双向交互通过才算成功。用户不再需要共享时，恢复原来的 IDE 设置并停用新增后端服务。

共享代理会把 IDE 的普通 `app-server` stdio 流量转为私有 Unix socket 上的 WebSocket；它不是把 `/use` 自动绑定到当前 IDE 标签页。若任务由 IDE 启动，原客户端专属的工具仍可能只能由 IDE 处理。

## 6. 附件与可选能力

- **私聊附件输出。** Codex 生成的图片和最终回复中明确链接的 JPG、PNG、GIF、WebP 会发成飞书图片消息。明确链接的 PDF、Word、Excel、PowerPoint 和 CSV 会以可下载的文件消息发送。附件需在桥接主机的当前会话工作区或 `/mnt/data` 中，Codex 生成的图片也可来自该会话的生图目录。每轮最多发送六个附件，单张图片上限为 10 MB，单份文件上限为 30 MB。图片和文件共用「获取与上传图片或文件资源」（`im:resource`）权限。 [飞书上传图片](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/image/create) · [飞书上传文件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/file/create)
- **全局问题**：`global_questions_enabled: true` 会扫描 `codex_home/sessions`，将同一 Codex 数据目录中其他活动会话的非敏感问题发到最近的已授权私聊，并通过 IDE 私有 IPC 回答。它涉及其他会话内容，且依赖未公开的 IDE IPC 格式；只在用户明确需要并实际验证后开启。普通桥接会话内的问题无需开启。
- **群聊**：申请飞书 `im:message.group_at_msg:readonly` 等当前平台要求的权限；可通过[获取机器人信息](https://open.feishu.cn/document/client-docs/bot-v3/bot-overview)查到该应用机器人的 `open_id` 并填入配置的 `bot_open_id`，让授权用户在群内 @机器人。回复对群成员可见，先获得用户对使用该群的明确意愿。
- **表情反馈**：开启 `message_feedback_enabled`，并按飞书[消息表情回复接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1)申请对应权限；无此权限时保持关闭即可。
- **飞书斜杠命令面板**：`python code/sync_commands.py --config runtime/config.json` 先列只读计划；用户需要面板注册时，再按应用权限要求运行同命令加 `--apply`。它会更改飞书应用的命令配置；普通文本 `/help` 等命令不依赖此面板。
- **服务器监控卡片**：复制 `examples/server-monitor.example.json` 到 `runtime/server-monitor.json` 并设 600，填写目标私聊 `chat_id`。配对并发送消息后，可在私有 `runtime/state.sqlite3` 的 `messages` 表中按已绑定用户和 `chat_type='p2p'` 找到对应聊天；有多个候选时请用户确认，不要把聊天记录打印到对话。先运行 `python code/server_monitor.py --sample` 验证本机采样；`--once` 会实际发卡并尝试置顶，持续运行默认每 30 秒更新。发卡、编辑和置顶权限应按飞书接口当前要求申请。该服务独立于桥接，启用前说明其会向目标聊天发送主机指标。
- **独立 webhook 通知**：`code/notify_feishu.py` 是单向可选补充。复制示例到 `runtime/feishu-notify.json`、设 600，让用户安全填写自定义机器人 webhook 和签名密钥，再按 `--help` 测试；要让其他 Codex 客户端自动调用，需按用户实际 `config.toml` 的 `notify` 设置配置。它可能与共享后端通知重复，且会发送最终回复内容，不要默认开启。

## 7. 故障排查与交付

- 无回复：检查进程及 `status.json` 的 `websocket_connected`、应用可用范围、是否已发布、长连接事件订阅、私聊绑定，以及消息是否为文本；避免同时运行相同应用的多个实例。
- `Feishu ... code=99991672` 等权限错误：按[官方权限排查](https://open.feishu.cn/document/faq/trouble-shooting/how-to-fix-the-99991672-error)及实际 API 页面申请缺失权限并发布；不要仅凭日志类型宣称具体权限已开通。
- Codex 初始化失败：检查真实二进制、登录用户、`CODEX_HOME`、工作区、网络和 app-server 版本。代理共享失败时检查配置中两处 socket 路径、私有目录和用户级服务，不要直接删除正在使用的 socket。
- 按钮不可用而 `/answer` 可用：检查回调是否单独配置为长连接并订阅 `card.action.trigger`，以及发布版本。
- 报告已配置项、实际验证项、未完成的飞书后台/审批/端到端步骤和恢复方法；不要披露任何凭据、绑定码、私人消息或会话 ID。

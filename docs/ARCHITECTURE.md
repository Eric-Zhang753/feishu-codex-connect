# 机制说明

飞书 SDK 用应用凭据建立出站长连接，接收 `im.message.receive_v1`；事件先检查来源、时效、类型和用户身份，再写入 SQLite 去重。未配置白名单时，首位持有一次性绑定码且从私聊发起绑定的用户成为 owner。普通消息进入串行队列，Codex app-server 返回的进度和结果通过飞书应用消息 API 以卡片发送。回合中断、交互问题和审批被映射为飞书命令或卡片回调。

默认使用独立 app-server 子进程；配置 `shared_socket` 后，桥接使用本项目代理连接持久的 Unix WebSocket 后端。`codex-shared` 将 IDE 的 app-server stdio 请求转接到同一后端，其他 CLI 命令交给真实 Codex。两个客户端仍有各自的连接和各自的会话选择；在飞书用 `/sessions`、`/use` 显式选择共享线程。提问回传有两种路径：共享代理通过当前安装 `runtime/question.sock` 转发 IDE 收到的问题；可选的 `global_questions.py` 扫描 `CODEX_HOME/sessions` 并通过 IDE 私有 IPC 查找会话所有者。后者默认关闭。

`runtime/config.json` 是桥接入口，其同级目录保存数据库、日志、状态、配对信息和本地问题套接字。共享代理通过 `CODEX_SHARED_ROOT` 寻找同目录的 `shared-runtime.json`。数据库含用户消息和结果；即使已绑定也应保密。`allowed_user_ids` 是用户 open_id 列表；为空时使用一次性配对。服务器异常重启后，已运行的桥接任务标记为中断，不自动重复执行；已排队任务按现有恢复逻辑继续。

代码默认要求新线程使用 `workspace-write` 沙箱和 `on-request` 审批；其他已存在会话的上下文、访问范围和工具能力由 Codex 及其配置决定。桥接仅对已识别的审批请求转交用户，不代替 Codex 的安全机制；不支持的 IDE 专属工具应由原客户端处理。消息正文和问答会经过飞书，使用者应自行确认聊天接收范围。

项目从真实运行代码提取，但没有复制原机器的凭据、配置、会话、运行记录、日志、服务文件或第三方 vendor 目录。版本锁见 `requirements.lock`。`sync_commands.py --apply`、监控发卡和 webhook 发送会修改或使用真实飞书资源，须在实际部署中按用户意图启用。修改原有系统服务、IDE 设置或上游应用前先核对影响。

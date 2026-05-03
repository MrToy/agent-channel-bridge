# Agent Channel Bridge

将 QQ 消息无缝路由到 AI Coding Agent（如 OpenCode、Claude Code），实现**在 QQ 里直接指挥 AI Agent 写代码**。

## 🎯 用途

你有没有过这样的场景：

- 在地铁上突然想到一个 bug 要修，但电脑不在身边
- 想给 AI Agent 安排一个任务，但不想开终端
- 团队成员在群里讨论代码，需要 AI 实时参与

**Agent Channel Bridge 就是来解决这个问题的。** 它把 QQ 变成 AI Agent 的遥控器：

| 场景 | 怎么做 |
|------|--------|
| 💬 给 Agent 派任务 | 在 QQ 私聊/群聊 @机器人 → Agent 自动执行 |
| 🔄 打断当前工作 | 发新消息会自动 Ctrl-C 中断 Agent 当前任务，切换到新指令 |
| 🧠 保持上下文 | Agent 的 session 会持久化保存，重启后自动恢复对话 |
| 📎 发代码/文件 | 图片自动转 base64、文件自动编码发送给 Agent |
| 👥 多 Agent 管理 | 一个机器人可以对接多个 Agent，不同群聊路由到不同 Agent |

## 📖 使用方式

### ⚡ 推荐：用 AI 帮你配置（最快）

Bridge 是一个 AI 工具，那当然**用 AI 来配置它**最方便。把下面这段 prompt 发给你的 AI coding agent（OpenCode、Claude Code 等）：

<details>
<summary>📋 点击展开 AI 配置 Prompt</summary>

> 请帮我完成 Agent Channel Bridge 的配置和启动。
>
> **任务：**
>
> 1. 从 https://github.com/MrToy/agent-channel-bridge 克隆项目
> 2. 安装 Python 依赖（websockets、pyyaml）
> 3. 检查 NapCat 是否已部署并运行在 localhost:3001
> 4. 编辑 config.yaml，填入我的 QQ 信息：
>    - 机器人 QQ 号：**YOUR_BOT_QQ**
>    - 机器人名字：**YOUR_BOT_NAME**
>    - 管理员 QQ 号：**YOUR_ADMIN_QQ**
>    - 需要对接的群号：**YOUR_GROUP_IDS**（多个群用逗号分隔）
>    - Agent 工作目录：**YOUR_AGENT_WORK_DIR**
>    - Agent 启动命令：**opencode acp**（或 claude --acp --stdio）
> 5. 启动 bridge（推荐用 tmux）
> 6. 验证启动成功：给我发一条测试消息
>
> 如果 NapCat 还没部署，请帮我部署 NapCat Docker 容器并完成 QQ 登录。

</details>

把上面方括号里的内容替换成你的实际信息，发给你的 AI Agent 就行。如果没有现成的 AI Agent，也可以用 ChatGPT/DeepSeek 等对话式 AI 生成 config.yaml 的内容，然后手动跑。

### 1. 安装依赖

```bash
pip install websockets pyyaml
```

### 2. 部署 NapCat

参考 [NapCat 文档](https://napcat.napneko.icu/) 部署 QQ 机器人，确保 WebSocket 服务在 `ws://localhost:3001`。

### 3. 配置

编辑 `config.yaml`：

```yaml
default:
  worker: my_agent

applications:
  qq:
    type: napcat
    bot_qq: '123456789'         # 你的机器人 QQ 号
    bot_name: MyBot              # 机器人名字
    ws_url: ws://localhost:3001  # NapCat WS 地址

routes:
  qq:private:987654321:         # 管理员 QQ
    name: MyAgent
    worker: my_agent
    admin: true                  # 可执行管理命令
  qq:group:11111111:            # 群聊
    name: MyAgent
    worker: my_agent

workers:
  my_agent:
    name: my_agent
    start_command: opencode acp  # 启动命令
    work_dir: /path/to/agent     # Agent 工作目录
```

### 4. 启动

```bash
python3 bridge.py
```

推荐用 tmux 运行（方便后台管理和查看日志）：

```bash
tmux new-session -d -s bridge 'python3 bridge.py'
```

### 5. 使用

**私聊：** 直接发消息给机器人，默认全部回复。

**群聊：** @机器人 才会触发回复，不 @ 不会被打扰。

**管理命令（仅 admin）：**

| 命令 | 作用 |
|------|------|
| `/status` | 查看所有 Worker 状态 |
| `/reset` | 重置当前对话 session |
| `/help` | 查看帮助 |

### 6. 配置修改

改完 `config.yaml` 后，**重启 bridge** 生效：

```bash
pkill -f bridge.py && python3 bridge.py
```

## ⚙️ 原理

```
┌──────────────────────────────────────────────────────────┐
│  QQ                                                        │
│  你发消息 → NapCat (WebSocket :3001)                      │
└──────────────────┬───────────────────────────────────────┘
                   │ OneBot v11 协议
                   ▼
┌──────────────────────────────────────────────────────────┐
│  Agent Channel Bridge (bridge.py)                         │
│                                                          │
│  1. 收到消息 → 解析类型（私聊/群聊/@）                      │
│  2. 路由匹配 → 找到对应的 Worker                          │
│  3. 打断发送 → Ctrl-C 当前任务 + send-keys 新消息           │
│  4. 监听回复 → 累积 agent_message_chunk → step_finish 完整 │
│  5. 回复发回 → WS → NapCat → QQ                           │
└──────────────────┬───────────────────────────────────────┘
                   │ ACP 协议 (JSON-RPC over stdio)
                   ▼
┌──────────────────────────────────────────────────────────┐
│  AI Coding Agent (OpenCode / Claude Code 等)              │
│                                                          │
│  - 独立 tmux 进程，每个 Worker 一个                        │
│  - 收到消息后自动执行（读文件、写代码、跑测试...）           │
│  - 实时流式回复，bridge 自动收集完整回复                     │
└──────────────────────────────────────────────────────────┘
```

### 核心技术：ACP 协议

ACP（Agent Communication Protocol）是一个 JSON-RPC over stdio 的协议，让外部程序能和 AI Agent 双向通信：

| 方法 | 作用 |
|------|------|
| `initialize` | 握手，声明客户端能力 |
| `session/create` | 创建新对话会话 |
| `session/resume` | 恢复已有对话会话 |
| `prompt` | 发送消息给 Agent（带图片/文件） |
| `tool/execute` | 直接让 Agent 执行工具 |
| `session/update` ← | **Agent 主动推送**（流式回复的关键） |

**流式回复机制：**
1. Bridge 发送 `prompt` 后，Agent 开始执行
2. Agent 每生成一段文本，就发一个 `session/update`（`agent_message_chunk`）
3. Agent 执行完毕，发 `step_finish`
4. Bridge 收集所有 chunk 拼成完整回复，发回 QQ

### Session 持久化

- Worker 的会话映射保存在 `work_dir/.bridge_sessions.json`
- Bridge 重启时会自动恢复所有 session（`session/resume`）
- 通过 `/reset` 命令可以重置指定 session

## 📋 路由规则详解

```
# 精确匹配（优先级最高）
qq:private:QQ号        → 指定私聊路由
qq:group:群号          → 指定群聊路由

# 默认匹配（优先级最低）
私聊无匹配 → default.worker
群聊@无匹配 → default.worker
```

## 🤖 支持的 Agent

任何支持 ACP 协议的 CLI agent：

- [OpenCode](https://github.com/opencode-ai/opencode) — `opencode acp`
- [Claude Code](https://github.com/anthropics/claude-code) — `claude --acp --stdio`

### 多 Agent 配置示例

```yaml
workers:
  coder:
    name: coder
    start_command: opencode acp
    work_dir: /home/user/coder-workspace
  reviewer:
    name: reviewer
    start_command: claude --acp --stdio
    work_dir: /home/user/reviewer-workspace

routes:
  qq:group:11111111:
    name: Coder
    worker: coder
  qq:group:22222222:
    name: Reviewer
    worker: reviewer
```

## 🏗 项目结构

```
agent-channel-bridge/
├── bridge.py           # 主程序 — WS 连接 + ACP 通信 + 路由
├── config.yaml         # 配置文件（路由、Worker、应用）
├── bridge.sh           # 进程管理脚本（start/stop/status/restart）
├── AGENTS.default.md   # Agent 默认角色设定模板
├── chat_logs/          # 聊天日志（自动按日期分文件）
├── logs/               # 运行日志 + RPC 日志
└── pids/               # PID 文件
```

## 🧩 扩展：开发自己的 Bridge Skill

Agent 侧可以通过 ACP `tool/execute` 或 skill 在回复中直接调用 WS API 发送消息，实现**主动推送**（不需要等回复）：

```
Agent → skill 调用 WS API → QQ 群主动推送消息
```

这在 Agent 长时间执行任务时特别有用——可以实时通知进度。

## License

MIT

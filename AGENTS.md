# AGENTS.md — Agent Channel Bridge 工作指南

## 📌 项目概览

Agent Channel Bridge 是一个将 QQ 消息路由到 AI Coding Agent（OpenCode、Claude Code 等）的桥梁。它通过 ACP 协议（JSON-RPC over stdio）与 Agent 子进程通信，NapCat 作为 QQ 协议层。

## 🏗 项目结构

```
agent-channel-bridge/
├── pyproject.toml              # 构建配置、依赖、CLI 入口
├── Makefile                    # make run / make test / make lint
├── bridge.sh                   # 进程管理（start/stop/restart/status）
├── Dockerfile                  # Docker 镜像构建
├── config.yaml                 # 本地配置文件（gitignored）
├── config.template.yaml        # 配置模板
├── AGENTS.default.md           # 默认 AI 角色模板（用于开箱体验）
├── AGENTS.md                   # 👈 本文件 — AI 工作指南
├── README.md                   # 用户文档（修改时同步更新）
├── src/
│   └── agent_channel_bridge/
│       ├── __init__.py         # 版本号 + logging 初始化
│       ├── __main__.py         # 入口、WS 主循环、管理命令
│       ├── config.py           # 配置加载、路由匹配（通配符）、聊天日志
│       ├── acp_worker.py       # ACP Worker — Agent 子进程管理
│       ├── worker_manager.py   # Worker 生命周期管理
│       ├── onebot.py           # OneBot v11 协议（消息构建/解析/API）
│       └── rpc_log.py          # JSON-RPC 日志
├── tests/
│   └── test_bridge.py          # 单元测试（36 个用例，覆盖路由/消息/解析）
└── CHANGELOG.md                # [可选] 变更日志
```

## 🔧 核心工作流程

### 1. 新增功能/修复 Bug 的标准流程

```
分析需求 → 写测试 → 实现 → 运行全部测试 → 更新文档 → 提交 PR
```

**必须遵守：**
- ✅ **先写测试再写代码**（测试驱动）
- ✅ **运行 `make test` 确保全部通过**（36 个用例）
- ✅ **运行 `make lint`**（ruff + mypy）
- ✅ **代码提交后签名并推送**（`config.yaml` 已 gitignored，注意检查）
- ✅ **同步更新 README.md**（特别要检查：配置示例、路由规则、架构图）

### 2. 测试规范

所有测试在 `tests/test_bridge.py`，使用 `pytest`。

**测试分类（参考现有测试）：**

| 测试类 | 测试内容 | 新增时注意 |
|--------|---------|-----------|
| `test_import` / `test_version` | 基础导入 | 加新模块时更新 |
| `TestRouteMatching` | 路由匹配逻辑 | 修改 `get_route()` 时必须覆盖：精确匹配、所有 8 种通配组合、优先级顺序、群聊@限制 |
| `TestBuildMessageSegments` | 消息段构建 | 修改 `_build_message_segments()` 必须覆盖：所有标签类型、混合内容、空内容、截断、边界情况 |
| `TestParseOnebot` | 协议解析 | 修改 `parse_onebot()` 必须覆盖：私聊/群聊、CQ at/文本 at、@剥离、空消息、发送者回退 |
| `TestRpcLog` | RPC 日志 | 修改 `rpc_log.py` 时更新 |

**运行测试：**
```bash
make test           # 常用
python -m pytest tests/ -v --tb=short   # 详细输出
```

### 3. 路由匹配规则（重要！）

`config.py` 中的 `get_route()` 是核心逻辑。匹配策略：

```
精确匹配 → 通配匹配（8 种通配组合按优先级依次尝试）
```

**候选顺序（以 `qq:group:123` 为例）：**
1. `qq:group:123` ← 精确
2. `qq:group:*`
3. `qq:*:123`
4. `qq:*:*`
5. `*:group:123`
6. `*:group:*`
7. `*:*:123`
8. `*:*:*`

**关键约束：** 群聊消息必须被 `@`（`is_mention=True`）才会匹配任意路由（包括 `*:*:*`）。私聊自动匹配。

**配置示例：**
```yaml
routes:
  qq:private:ADMIN_QQ:       # 精确
    worker: agent1
    admin: true
  qq:private:*:               # 所有私聊兜底
    worker: agent1
  *:*:*:                      # 全兜底
    worker: agent1
```

**修改 `get_route()` 后必须更新：**
1. 测试：补充精确/通配/优先级用例
2. README：更新"路由规则详解"章节的匹配示例
3. `config.template.yaml`：更新示例路由（如果新增了配置格式）

### 4. 消息段构建

Agent 回复中可嵌入媒体标签，`_build_message_segments()` 解析后转为 OneBot v11 消息段：

| 标签 | 对应 OneBot 类型 | 本地文件支持 |
|------|-----------------|------------|
| `<img>URL</img>` | `image` | ✅ 自动 base64 |
| `<audio>URL</audio>` | `record` | ❌ 仅 URL |
| `<file>URL</file>` | `file` | ✅ 自动 base64 |

URL 中的换行/空白会被自动清理，标签大小写不敏感。

### 5. 配置管理

- `config.yaml` 已加入 `.gitignore`，**切勿手动移除 gitignore**
- `config.template.yaml` 是配置模板，修改配置格式时必须同步更新模板
- 配置格式变更后，README 的配置示例必须同步更新

### 6. OneBot 协议解析关键点

- 群聊 `@` 检测：优先 CQ 码 `message[].type == "at"`，回退到文本 `@bot_name`
- `@` 前缀会在解析时剥离
- 发送者名称回退链：`nickname → card → "QQ{user_id}"`

### 7. 文档同步清单

修改以下任一代码后，必须检查并更新对应文档：

| 代码修改 | 需要更新的文档 |
|---------|--------------|
| `get_route()` / 路由逻辑 | README「路由规则详解」+ `config.template.yaml` |
| 配置格式（yaml 字段） | README「配置」示例 + `config.template.yaml` |
| ACP 方法/协议变更 | README「核心技术：ACP 协议」表格 |
| 消息格式/标签 | README「消息格式」+ Agent 系统提示模板（`_do_send_prompt`） |
| 新增管理命令 | README「管理命令」表格 |
| 项目结构变更 | README「项目结构」树形图 |
| 添加新模块 | `tests/test_bridge.py` 的 `test_import` 增加导入验证 |

## ⚠️ 常见 Pitfalls

1. **`config.yaml` 不要提交！** 它已在 `.gitignore` 中，更新配置模板请改 `config.template.yaml`
2. **YAML 通配符引号：** `*:*:*` 在 YAML 中会被解析为锚点，必须加引号写成 `'*:*:*'`
3. **日志配置在 `__init__.py`：** 使用 `force=True`，可能覆盖其他模块的 logger
4. **`_ws_conn` 是模块级全局变量：** 在 `config.py` 中定义，`__main__.py` 和 `onebot.py` 共享引用，重连时会有窗口期
5. **`_prompt_msg_map` 无超时清理：** 后台 prompt 如果不返回，map 会泄漏（潜在风险）
6. **`auto_test()` 已移除：** 不要再添加自动测试消息，会消耗 API 额度

## 🧪 测试辅助

测试中路由匹配使用 `_inject_routes()` 和 `_reset_config()` 注⼊配置：

```python
def _inject_routes(my_routes):
    import agent_channel_bridge.config as cfg
    cfg.config["routes"] = my_routes
```

```python
def _reset_config():
    import agent_channel_bridge.config as cfg
    cfg.config.clear()
```

测试 OneBot 解析使用 `_make_msg()` 辅助函数构建模拟消息体。

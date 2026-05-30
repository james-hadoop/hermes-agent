# Hermes Agent 核心执行流程可视化

> 从 `hermes_cli/main.py` 入口到各组件调用的完整流程图

## 一、整体架构层级

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CLI 入口层 (用户交互)                              │
│  hermes_cli/main.py ─── hermes_cli/cli.py (HermesCLI 交互循环)      │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent 层 (会话逻辑)                                │
│  run_agent.py (AIAgent) ─── agent/ (模块化组件)                      │
│    ├── agent_init.py       (初始化)                                  │
│    ├── conversation_loop.py (主循环)                                  │
│    ├── transports/         (传输适配器)                               │
│    └── auxiliary_client.py (辅助推理后端)                             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    工具 & 执行层                                      │
│  model_tools.py ─── tools/registry.py ─── tools/*.py                 │
│  toolsets.py (工具集分组)                                             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    基础设施层                                         │
│  ├── config:    hermes_cli/config.py + hermes_constants.py           │
│  ├── state:     hermes_state.py (SQLite + FTS5)                     │
│  ├── memory:    agent/memory_manager.py + memory_provider.py         │
│  ├── prompt:    agent/system_prompt.py + prompt_builder.py           │
│  └── logging:   hermes_logging.py                                   │
└─────────────────────────────────────────────────────────────────────┘
```

## 二、详细执行流程 (Mermaid 图)

```mermaid
flowchart TD
    %% ===== CLI Entry =====
    subgraph CLI["CLI 入口层"]
        A1["main() @ hermes_cli/main.py:11258"]
        A2["Bootstrap 阶段"]
        A3["配置加载阶段"]
        A4["Argparse 分发阶段"]
        
        A1 --> A2
        
        A2 --> A2a["hermes_bootstrap (Windows UTF-8 stdio)"]
        A2 --> A2b["_suppress_mouse_residue_early (TUI)"]
        A2 --> A2c["_apply_profile_override (HERMES_HOME)"]
        A2 --> A2d["load_hermes_dotenv (.env)"]
        A2 --> A2e["setup_logging (agent.log / errors.log)"]
        A2 --> A2f["_sync_bundled_skills_for_startup"]
        
        A2 --> A3
        
        A3 --> A3a["hermes_cli/config.py → load_config()"]
        A3 --> A3b["hermes_constants.py → get_hermes_home()"]
        A3 --> A3c["hermes_cli/config.py → get_config_path()"]
        
        A3 --> A4
        
        A4 --> A4a{"命令判断"}
        A4a -->|chat / 默认| CMD_CHAT["cmd_chat(args)"]
        A4a -->|setup| CMD_SETUP["cmd_setup()"]
        A4a -->|gateway| CMD_GW["cmd_gateway()"]
        A4a -->|model| CMD_MODEL["cmd_model()"]
        A4a -->|config| CMD_CONFIG["cmd_config()"]
        A4a -->|session| CMD_SESS["cmd_sessions()"]
        A4a -->|tui| CMD_TUI["--tui 走 Node.js Ink"]
        A4a -->|其他| CMD_OTHER["hermes_cli/*_cmd.py"]
    end
    
    %% ===== Chat Path =====
    subgraph CHAT["Chat 交互层"]
        CMD_CHAT --> B1["HermesCLI() @ hermes_cli/cli.py"]
        B1 --> B2["load_cli_config()"]
        B1 --> B3{"会话从哪里开始?"}
        B3 -->|--resume SID| B3a["SessionDB.get_session() @ hermes_state.py"]
        B3 -->|-c "title"| B3b["SessionDB.resolve_session_by_title()"]
        B3 -->|新会话| B3c["生成新 session_id"]
        
        B2 --> B4["交互主循环"]
        B4 --> B4a{"用户输入"}
        B4a -->|斜杠命令| B4a1["process_command() → COMMAND_REGISTRY @ hermes_cli/commands.py"]
        B4a1 --> B4a1a["/help, /quit, /clear, /resume, /copy..."]
        B4a1 --> B4a1b["/skill → scan ~/.hermes/skills/"]
        B4a1 --> B4a1c["/background → 后台任务"]
        B4a -->|普通消息| B4a2["AIAgent.chat() / run_conversation()"]
    end
    
    %% ===== Agent Init =====
    subgraph INIT["Agent 初始化 run_agent.py + agent_init.py"]
        B4a2 --> C1["AIAgent.__init__()"]
        C1 --> C1a["init_agent(self, ...) @ agent/agent_init.py"]
        C1a --> C1a1["设置 model / provider / base_url"]
        C1a --> C1a2["API 模式自动检测"]
        C1a2 -->|OpenAI 兼容| CT["chat_completions @ agent/transports/chat_completions.py"]
        C1a2 -->|Anthropic 原生| AM["anthropic_messages @ agent/transports/anthropic_messages.py"]
        C1a2 -->|OpenAI Responses| CR["codex_responses @ agent/transports/codex_responses.py"]
        C1a2 -->|AWS Bedrock| BC["bedrock_converse @ agent/transports/bedrock_converse.py"]
        C1a --> C1a3["工具发现 → model_tools.get_tool_definitions()"]
        C1a --> C1a4["传输适配器预热 → _get_transport()"]
        C1a --> C1a5["内存管理器 → MemoryManager()"]
        C1a --> C1a6["系统提示构建 → _build_system_prompt()"]
        C1a --> C1a7["OpenRouter 预热线程 (后台)"]
    end
    
    %% ===== Conversation Loop =====
    subgraph LOOP["对话主循环 agent/conversation_loop.py"]
        C1 --> D1["run_conversation(agent, user_message)"]
        D1 --> D1a["_ensure_db_session() → SessionDB"]
        D1 --> D1b["重置重试计数器"]
        D1 --> D1c["消息预处理 (surrogate 清理等)"]
        
        D1 --> D2{"while 迭代预算 >= 0 且未中断"}
        
        D2 --> D3["构建 API 请求"]
        D3 --> D3a["记忆预制 → memory_manager.prefetch_all()"]
        D3 --> D3a1["agent/memory_manager.py → build_memory_context_block()"]
        D3a1 --> D3a2["内置 memory 插件"]
        D3a1 --> D3a3["外部提供器 (Honcho/Mem0 等)"]
        
        D3 --> D3b["系统提示 → agent/system_prompt.py"]
        D3b --> D3b1["稳定层: SOUL.md / 工具指导 / 技能提示 / 环境提示"]
        D3b1 --> D3b1a["agent/prompt_builder.py"]
        D3b1a --> D3b1a1["load_soul_md()"]
        D3b1a --> D3b1a2["build_skills_system_prompt() → ~/.hermes/skills/"]
        D3b1a --> D3b1a3["build_context_files_prompt() → AGENTS.md + .cursorrules"]
        D3b1a --> D3b1a4["build_environment_hints() → OS/Shell 信息"]
        
        D3b --> D3b2["上下文层: caller system_message + AGENTS.md"]
        D3b --> D3b3["易变层: 时间戳 / 会话信息"]
        
        D3 --> D3c["消息历史 → conversation_history"]
        D3 --> D3d["工具定义 → get_tool_definitions()"]
        
        D3 --> D4["API 调用"]
        D4 --> D4a["transport.build_kwargs() @ agent/transports/*"]
        D4a --> D4a1["convert_messages()"]
        D4a --> D4a2["convert_tools()"]
        D4a --> D4a3["组装完整参数 (model, max_tokens, reasoning...)"]
        
        D4a --> D4b["HTTP 请求 → provider SDK (OpenAI / Anthropic / Bedrock...)"]
        
        D4b --> D5{"响应中有 tool_calls?"}
        D5 -->|是| D6["工具分发循环"]
        D5 -->|否| D7["返回最终文本响应"]
        
        D6 --> D6a["handle_function_call() @ model_tools.py"]
        D6a --> D6a1["registry.dispatch() @ tools/registry.py"]
        D6a1 --> D6a2["对应 tools/*.py 处理函数"]
        
        D6a2 --> D6b["工具结果拼接"]
        D6b --> D6c["运行记忆同步 → memory_manager.sync_all()"]
        D6c --> D6d["返回循环顶 D2"]
        
        D2 -->|预算耗尽| D8["预算耗尽处理 → 最后调一次"]
        D8 --> D9{"模型输出文本?"}
        D9 -->|是| D10["返回最终响应"]
        D9 -->|否| D11["强制请求总结"]
        
        D10 --> D12["后置处理"]
        D12 --> D12a["保存会话 @ hermes_state.py"]
        D12 --> D12b["更新 token 计数"]
        D12 --> D12c["保存轨迹 (如启用)"]
        D12 --> D12d["后台记忆/技能审查 (线程)"]
        
        D12 --> D13["返回 {final_response, messages}"]
    end
    
    %% ===== Tool System =====
    subgraph TOOLS["工具系统"]
        T1["tools/registry.py (中心注册表)"]
        T2["tools/*.py (每个工具文件 self-register)"]
        T3["model_tools.py (编排层)"]
        T4["toolsets.py (工具集定义)"]
        
        T1 --> T2
        T3 --> T1
        T3 --> T4
    end
    
    %% ===== State / DB =====
    subgraph STATE["持久化层"]
        S1["hermes_state.py"]
        S1 --> S1a["SessionDB (SQLite)"]
        S1a --> S1a1["会话元数据"]
        S1a --> S1a2["消息历史"]
        S1a --> S1a3["FTS5 全文搜索"]
        S1a --> S1a4["压缩链 (parent_session_id)"]
    end
    
    %% ===== Memory =====
    subgraph MEMORY["记忆系统"]
        M1["agent/memory_manager.py (MemoryManager)"]
        M1 --> M1a["agent/memory_provider.py (ABC)"]
        M1a --> M1a1["内置 memory 工具"]
        M1a --> M1a2["插件: Honcho / Mem0 / SuperMemory"]
        M1 --> M1b["agent/think_scrubber.py (推理标签擦除)"]
        M1 --> M1c["StreamingContextScrubber (流式上下文字段擦除)"]
    end
    
    %% ===== Auxiliary =====
    subgraph AUX["辅助推理系统"]
        AX1["agent/auxiliary_client.py"]
        AX1 --> AX2a["会话搜索摘要"]
        AX1 --> AX2b["上下文压缩"]
        AX1 --> AX2c["视觉分析"]
        AX1 --> AX2d["网页提取"]
        AX1 --> AX2e["浏览器视觉"]
        
        AX2a --> AX3["解析链: 主模型 → OpenRouter → Portal → Anthropic → 其他"]
    end

    %% ===== Connections =====
    D6a2 -.- TOOLS
    D12a -.- STATE
    D3a1 -.- MEMORY
    D12d -.- AUX
    
    %% Styling
    classDef entry fill:#4a90d9,color:#fff,stroke:#2a6fb0
    classDef process fill:#50b86c,color:#fff,stroke:#2d8a4a
    classDef component fill:#e67e22,color:#fff,stroke:#c06418
    classDef data fill:#8e44ad,color:#fff,stroke:#6c3483
    classDef decision fill:#f39c12,color:#000,stroke:#d68910
    
    class A1,CMD_CHAT entry
    class C1,INIT,LOOP process
    class D3b,TOOLS,STATE,MEMORY,AUX component
    class T1,T2,T3,T4,S1,M1 data
    class A4a,B3,B4a,D5,D9 decision
```

## 三、关键路径文字描述

### 路径 1: 默认 Chat 启动

```
hermes (无参数)
  └─ main() @ hermes_cli/main.py
       ├─ Bootstrap → UTF-8 stdio, 配置 .env, logging
       ├─ argparse → 默认走 cmd_chat()
       └─ HermesCLI() @ hermes_cli/cli.py
            ├─ load_cli_config() → ~/.hermes/config.yaml
            ├─ AIAgent.__init__() → agent_init.py
            │    ├─ 解析 provider/model/base_url
            │    ├─ 自动检测 api_mode → transports/*
            │    ├─ 发现工具 → model_tools → registry
            │    ├─ 构建系统提示 → system_prompt.py → prompt_builder.py
            │    └─ 初始化 MemoryManager
            └─ 交互循环 (prompt_toolkit)
                 └─ 用户消息 → AIAgent.run_conversation()
                      └─ conversation_loop.py
                           ├─ 记忆预制 → memory_manager
                           ├─ API 调用 → transport.build_kwargs()
                           ├─ 工具循环 → handle_function_call() → registry.dispatch()
                           ├─ 保存会话 → SessionDB (SQLite)
                           └─ 返回最终响应
```

### 路径 2: Setup → Chat 启动

```
hermes setup
  └─ cmd_setup() @ hermes_cli/main.py
       ├─ 交互式配置向导
       │    ├─ 选择 provider (OpenRouter / Anthropic / OpenAI / 自定义...)
       │    ├─ 输入 API Key
       │    ├─ 选择默认模型
       │    ├─ 配置 toolsets
       │    └─ 可选消息平台配置
       ├─ 写入 ~/.hermes/config.yaml
       └─ 启动 HermesCLI → 同上 Chat 流程
```

### 路径 3: Gateway 启动

```
hermes gateway
  └─ cmd_gateway() @ hermes_cli/main.py
       └─ gateway/run.py
            ├─ 加载配置 (平台列表)
            ├─ 为每个平台创建适配器
            │    ├─ Telegram / Discord / Slack / WhatsApp
            │    ├─ Matrix / Signal / Mattermost
            │    ├─ 微信 / 飞书 / 钉钉 / 企业微信
            │    └─ API Server / Webhook
            ├─ 启动事件循环
            └─ 每条消息 → 创建 AIAgent → run_conversation()
```

### 路径 4: TUI 启动

```
hermes --tui
  └─ main() @ hermes_cli/main.py
       ├─ _suppress_mouse_residue_early()
       ├─ 检查 Node.js / npm
       ├─ npm install (如需要)
       ├─ npm run build (如源码变更)
       └─ hermes --tui
            ├─ Node.js 进程 (Ink UI)
            │    ├─ 渲染 transcript / composer / prompts
            │    └─ JSON-RPC over stdio ↔ Python tui_gateway
            └─ Python 进程 (tui_gateway)
                 └─ AIAgent + 工具 + 会话
```

## 四、核心文件依赖关系

```
工具链:
  tools/registry.py  (无依赖 — 所有工具文件导入它)
       ↑
  tools/*.py  (调用 registry.register() 自注册)
       ↑
  model_tools.py  (导入 registry + 所有工具模块 → 触发发现)
       ↑
  run_agent.py / cli.py / batch_runner.py / 环境适配器

传输链:
  agent/transports/base.py  (抽象基类 ProviderTransport)
       ↑
  agent/transports/chat_completions.py  (OpenAI 兼容格式)
  agent/transports/anthropic_messages.py  (Anthropic 原生格式)
  agent/transports/codex_responses.py  (OpenAI Responses API)
  agent/transports/bedrock_converse.py  (AWS Bedrock)

初始化链:
  run_agent.py (AIAgent 类)
       ↑
  agent/agent_init.py (init_agent 函数 — 提取的 __init__ 主体)
       ↑
  hermes_cli/config.py / hermes_constants.py / agent/iteration_budget.py
  agent/memory_manager.py / agent/context_compressor.py
  agent/model_metadata.py

对话循环链:
  run_agent.py (AIAgent.run_conversation 转发器)
       ↑
  agent/conversation_loop.py (run_conversation 主体)
       ↑
  agent/transports/* / model_tools.py
  agent/memory_manager.py / agent/auxiliary_client.py
  agent/error_classifier.py / agent/retry_utils.py
  hermes_state.py / hermes_logging.py
```

## 五、各组件文件路径速查

| 组件 | 文件路径 | 职责 |
|------|---------|------|
| CLI 入口 | `hermes_cli/main.py` | `main()` 入口，argparse 分发所有子命令 |
| 交互 CLI | `hermes_cli/cli.py` | `HermesCLI` 类，prompt_toolkit 交互循环 |
| 核心 Agent | `run_agent.py` | `AIAgent` 类，对话和工具调用核心 |
| Agent 初始化 | `agent/agent_init.py` | `init_agent()` — 提取的 __init__ 主体 |
| 对话循环 | `agent/conversation_loop.py` | `run_conversation()` — 主循环逻辑 |
| 传输抽象 | `agent/transports/base.py` | `ProviderTransport` 抽象基类 |
| Chat 传输 | `agent/transports/chat_completions.py` | OpenAI 兼容格式适配 |
| Config | `hermes_cli/config.py` | 配置管理，load/save/migrate |
| 常量 | `hermes_constants.py` | 路径、环境检测、网络偏好 |
| 工具注册表 | `tools/registry.py` | 工具注册和发现 |
| 工具编排 | `model_tools.py` | 工具定义聚合、分发 |
| 工具集 | `toolsets.py` | 工具集定义和管理 |
| 记忆管理器 | `agent/memory_manager.py` | 内置记忆 + 外部提供器编排 |
| 记忆抽象 | `agent/memory_provider.py` | 记忆提供器抽象基类 |
| 思考擦除 | `agent/think_scrubber.py` | 流式推理标签状态机 |
| 会话状态 | `hermes_state.py` | SQLite + FTS5 会话存储 |
| 系统提示 | `agent/system_prompt.py` | 系统提示组装 (三层) |
| 提示构建器 | `agent/prompt_builder.py` | 身份、技能、平台提示等 |
| 辅助客户端 | `agent/auxiliary_client.py` | 侧任务推理后端 |
| 提示缓存 | `agent/prompt_caching.py` | Anthropic 提示缓存策略 |
| 日志 | `hermes_logging.py` | 日志初始化 |
| 错误分类 | `agent/error_classifier.py` | API 错误分类和重试策略 |
| 重试工具 | `agent/retry_utils.py` | 退避重试逻辑 |
| 上下文压缩 | `agent/context_compressor.py` | 长上下文压缩 |
| 轨迹 | `agent/trajectory.py` | 会话轨迹保存 |
| 消息清理 | `agent/message_sanitization.py` | 字符清理和修复 |
| 工具守卫 | `agent/tool_guardrails.py` | 工具调用安全守卫 |
| 插件 | `plugins/*/` | 外部插件 (memory, kanban, 平台...) |
| Gateway | `gateway/run.py` | 消息网关 |
| TUI 前端 | `ui-tui/src/` | Ink (React) 终端 UI |
| TUI 后端 | `tui_gateway/` | Python JSON-RPC 后端 |
| Slash 命令 | `hermes_cli/commands.py` | COMMAND_REGISTRY 中心注册 |
| 皮肤引擎 | `hermes_cli/skin_engine.py` | CLI 主题系统 |
| ACP 适配器 | `acp_adapter/` | 编辑器集成 (VS Code / Zed) |
| Cron | `cron/` | 定时任务调度器 |

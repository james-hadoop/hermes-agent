# DIY Agent — 基于 Hermes Agent 构建自定义 AI Agent 顶层设计

> **设计目标**：基于 hermes-agent 开源项目的架构模式，构建一个轻量级、可扩展的自定义 AI Agent 系统，覆盖用户输入、意图推理、LLM 集成与执行、状态管理、记忆管理等核心能力。
>
> **设计哲学**：复用 hermes-agent 经过生产验证的架构模式（分层解耦、自注册工具、传输层适配器、记忆管理），但只取所需的核心骨架，去掉网关/TUI/多平台等与场景无关的模块。

---

## 目录

- [1. 整体架构概览](#1-整体架构概览)
- [2. 模块分解与关键设计](#2-模块分解与关键设计)
  - [2.1 Core：Agent 核心引擎 (mini-agent-core)](#21-coreagent-核心引擎-mini-agent-core)
  - [2.2 Transport：LLM 传输层适配器 (mini-transport)](#22-transportllm-传输层适配器-mini-transport)
  - [2.3 Tools：工具注册与执行 (mini-tools)](#23-tools工具注册与执行-mini-tools)
  - [2.4 Memory：记忆管理 (mini-memory)](#24-memory记忆管理-mini-memory)
  - [2.5 State：状态持久化 (mini-state)](#25-state状态持久化-mini-state)
  - [2.6 Prompt：提示词工程 (mini-prompt)](#26-prompt提示词工程-mini-prompt)
- [3. 核心流程详解：用户输入→Action 决策](#3-核心流程详解用户输入action-决策)
- [4. 代码复用策略：哪些直接拿，哪些改，哪些重写](#4-代码复用策略哪些直接拿哪些改哪些重写)
- [5. 逐个功能对照：hermes-agent 能力映射表](#5-逐个功能对照hermes-agent-能力映射表)
- [6. 项目启动指南](#6-项目启动指南)
- [7. 附录：架构决策记录 (ADR)](#7-附录架构决策记录-adr)

---

## 1. 整体架构概览

### 1.1 模块依赖关系

```
┌─────────────────────────────────────────────────────────────────┐
│                       项目入口和用户接口                          │
│   CLI / API Server / SDK                                        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  mini-agent-core (Agent 引擎)                                    │
│                                                                  │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────┐  │
│  │ conversation │   │ system_prompt│   │ iteration_budget     │  │
│  │ _loop        │──▶│ _builder     │   │ (线程安全计数器)      │  │
│  └──────┬───────┘   └──────────────┘   └──────────────────────┘  │
│         │                                                       │
│  ┌──────▼──────────────────────────────────────────────────┐    │
│  │  turn_preparer (消息构建、上下文注入、消息修复)            │    │
│  └──────┬──────────────────────────────────────────────────┘    │
│         │                                                       │
│  ┌──────▼──────────────────────────────────────────────────┐    │
│  │  response_processor (finish_reason→决策分支)             │    │
│  │  ├── length_handler (截断→continuation/partial)          │    │
│  │  ├── tool_handler (工具验证→执行)                        │    │
│  │  └── text_handler (空响应恢复→final)                     │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
┌───────────────┐  ┌──────────────┐  ┌──────────────┐
│ mini-transport │  │  mini-tools  │  │ mini-memory  │
│ (LLM 传输层)   │  │ (工具注册)   │  │ (记忆管理)   │
└───────────────┘  └──────────────┘  └──────────────┘
                           │
                    ┌──────▼──────┐
                    │ mini-state  │
                    │ (SQLite 持久化)│
                    └─────────────┘
```

### 1.2 数据流（单轮对话）

```
用户输入 → run_conversation()
  │
  ├── [MemoryManager] 记忆预取 (背景回忆)
  ├── [SystemPrompt]  构建系统提示词 (缓存复用)
  ├── [TurnPreparer]  消息组装、修复角色交替、注入上下文
  ├── [Transport]     API 调用 (流式/非流式)
  │
  ├── 响应验证 (validate_response)
  │     └── 无效 → retry_with_backoff → fallback_promotion
  │
  ├── finish_reason 提取 (transport.normalize_response)
  │
  ├── finish_reason == "length" → 截断处理
  │     ├── 思考耗尽 → 友好提示
  │     ├── 有文本 → continuation (3次)
  │     ├── 有 tool_calls → retry (1次)
  │     └── 全空 → rollback
  │
  ├── 核心决策分支:
  │     ├── 有 tool_calls → 验证→执行工具→continue
  │     └── 无 tool_calls → 空响应恢复→break
  │
  └── 收尾: 记忆同步 + 状态持久化 + 返回结果
```

---

## 2. 模块分解与关键设计

### 2.1 Core：Agent 核心引擎 (mini-agent-core)

**对应 hermes-agent 文件**: `run_agent.py` + `agent/agent_init.py` + `agent/conversation_loop.py`

#### 核心思想

继承 hermes-agent 的**薄构造+厚初始化**模式。`AIAgent.__init__` 只保存参数，在首次 `chat()` 或 `run_conversation()` 时触发 `init_agent()` 完成全部初始化。

#### 设计

```python
class Agent:
    """DIY Agent 核心类"""

    def __init__(self, **config):
        # 仅保存参数 —— 不导入任何重模块
        self._raw_config = config
        self._initialized = False

    def _init(self):
        """惰性初始化（首次调用时触发）"""
        if self._initialized:
            return
        # 初始化所有子系统
        self.state = StateStore(self._raw_config.pop("state_path", None))
        self.memory = MemoryManager()
        self.transport = TransportFactory.create(self._raw_config)
        self.tools = ToolRegistry()
        self.prompt_builder = SystemPromptBuilder(self)
        self.budget = IterationBudget(self._raw_config.get("max_iterations", 90))
        self._initialized = True

    def run_conversation(self, user_message: str, ...) -> dict:
        self._init()
        # ... 见 3. 核心流程
```

#### 关键实例属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `state` | StateStore | SQLite 会话存储 |
| `memory` | MemoryManager | 外部记忆管理器 |
| `transport` | BaseTransport | LLM API 传输层 |
| `tools` | ToolRegistry | 工具注册表 |
| `prompt_builder` | SystemPromptBuilder | 系统提示词构建器 |
| `budget` | IterationBudget | 迭代预算计数器 |
| `session_messages` | list | 当前会话消息列表 |
| `session_id` | str | 当前会话 ID |
| `model` | str | 当前模型 |
| `provider` | str | 当前提供商 |

#### 从 hermes-agent 复用的模式

- **薄构造 + init_agent() 惰性初始化** — 保持构造函数极简，耗时操作延迟到首次调用
- **IterationBudget** 线程安全计数器 (`agent/iteration_budget.py`，62行，几乎原样复制)
- **前向引用模式** — 在模块底部放 `run_conversation()` 导出函数，避免循环导入

---

### 2.2 Transport：LLM 传输层适配器 (mini-transport)

**对应 hermes-agent 文件**: `agent/transports/base.py` + `agent/transports/chat_completions.py` + `agent/auxiliary_client.py`（部分）

#### 设计思想

Adapter 模式。每个 API 提供商有自己的消息格式、工具格式、响应结构。Transport 层屏蔽这些差异，对外暴露统一接口。

#### 接口定义

```python
class BaseTransport(ABC):
    """LLM 传输层适配器基类"""

    @abstractmethod
    def api_mode(self) -> str: ...

    @abstractmethod
    def build_kwargs(self, model, messages, tools, **params) -> dict:
        """构建 SDK 调用参数"""

    @abstractmethod
    def normalize_response(self, response, **kwargs) -> NormalizedResponse:
        """标准化响应格式"""

    def validate_response(self, response) -> bool:
        """验证响应是否有效"""
```

#### NormalizedResponse 结构

```python
@dataclass
class NormalizedResponse:
    content: str | None          # 文本内容
    tool_calls: list[ToolCall]   # 工具调用列表
    finish_reason: str           # "stop" | "length" | "tool_calls"
    usage: Usage | None          # token 用量

@dataclass
class ToolCall:
    id: str | None
    name: str
    arguments: str  # JSON string
    provider_data: dict | None

@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
```

#### 内置传输实现

| 实现 | 适用场景 | 说明 |
|------|----------|------|
| `ChatCompletionsTransport` | OpenAI / OpenRouter / DeepSeek / 本地 Ollama | 标准 OpenAI 兼容 API |
| `AnthropicTransport` | Anthropic Claude | Anthropic Messages API |
| `CustomTransport` | 自定义 API | 通过配置扩展 |

#### 从 hermes-agent 复用的代码

- **`agent/transports/base.py`** (89行) — ProviderTransport 抽象基类，几乎原样保留
- **`agent/transports/types.py`** (162行) — NormalizedResponse、ToolCall、Usage 数据类，直接复制
- **`agent/transports/chat_completions.py`** — 精简版（去掉 Gemini thinkingConfig / Moonshot / Developer Role 等特殊处理，保留核心的 build_kwargs 和 normalize_response）
- **`agent/auxiliary_client.py`** — 不使用其完整 fallback 链，只取其 `call_llm()` 核心逻辑做"辅助模型调用"

---

### 2.3 Tools：工具注册与执行 (mini-tools)

**对应 hermes-agent 文件**: `tools/registry.py` + `model_tools.py` + `toolsets.py`

#### 设计思想

自注册（Self-registration）。每个工具文件独立声明自己的 schema、handler、所属 toolset。核心注册表负责发现和聚合。工具执行逻辑集中在 `tool_executor.py`。

#### ToolRegistry

```python
class ToolRegistry:
    """工具注册表（线程安全单例）"""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._lock = threading.RLock()
        self._generation = 0

    def register(self, name, toolset, schema, handler, **kwargs):
        """工具自注册（在工具模块 import 时调用）"""

    def get_definitions(self, enabled_toolsets=None) -> list:
        """获取 OpenAI-format tool schemas"""

    def dispatch(self, name, args, task_id=None) -> str:
        """按名称调用工具处理函数"""

    def discover(self, tools_dir: str = None):
        """自动发现并导入工具模块"""
```

#### ToolEntry

```python
@dataclass
class ToolEntry:
    name: str
    toolset: str           # 所属工具集
    schema: dict           # OpenAI function schema
    handler: callable      # 调用处理函数
    check_fn: callable | None = None  # 可用性检查
    is_async: bool = False
    description: str = ""
    emoji: str = "🔧"
```

#### 工具集 (Toolsets)

参考 hermes-agent 的 `toolsets.py` 工具集概念，允许按场景分组：

```python
# 预定义工具集
_TOOLSETS = {
    "core":      ["web_search", "web_extract", "terminal", "read_file", "write_file", "patch", "search_files"],
    "code":      ["execute_code", "terminal", "read_file", "write_file", "patch", "search_files"],
    "research":  ["web_search", "web_extract", "read_file", "search_files"],
    "file":      ["read_file", "write_file", "patch", "search_files"],
    "memory":    ["memory", "session_search", "todo"],
    "full":      None,  # 所有工具
}
```

#### 工具自注册模式

```python
# tools/web_search.py
registry = ToolRegistry()

def handle_web_search(args):
    """搜索网络"""
    ...

registry.register(
    name="web_search",
    toolset="core",
    schema={...},  # OpenAI function schema
    handler=handle_web_search,
    description="Search the web for information",
    emoji="🌐",
)
```

#### 工具发现机制

```python
def discover_tools(tools_dir: str):
    """导入所有自注册工具模块"""
    path = Path(tools_dir)
    for pyfile in sorted(path.glob("*.py")):
        if pyfile.name in ("__init__.py", "registry.py"):
            continue
        if not _contains_register_call(pyfile):
            continue
        importlib.import_module(f"tools.{pyfile.stem}")
```

#### 工具执行（串行/并行）

```python
class ToolExecutor:
    """工具调用执行器"""

    @staticmethod
    def execute(tool_calls: list[ToolCall], registry: ToolRegistry) -> list[dict]:
        """执行工具调用"""
        if _should_parallelize(tool_calls):
            return _execute_concurrent(tool_calls, registry)
        return _execute_sequential(tool_calls, registry)

    @staticmethod
    def _should_parallelize(tool_calls: list[ToolCall]) -> bool:
        """判定是否可并行（读操作并行，写操作串行）"""
        ...
```

#### 从 hermes-agent 复用的代码

- **`tools/registry.py`** (589行) — ToolRegistry + ToolEntry，核心约 200 行，精简后直接使用
- **`toolsets.py`** (882行) — 工具集概念，DIY 版只需 ~100 行精简版
- **`model_tools.py`** 中的 `_should_parallelize_tool_batch` 逻辑 (`agent/tool_dispatch_helpers.py`)
- **async bridging** — 每个线程一个持久 event loop 防 "Event loop is closed"

---

### 2.4 Memory：记忆管理 (mini-memory)

**对应 hermes-agent 文件**: `agent/memory_manager.py` + `agent/memory_provider.py` + `run_agent.py` 中的 `memory` 工具

#### 设计思想

Manager + Provider 模式。`MemoryManager` 负责任务编排（预取、同步、系统提示词注入），`MemoryProvider` 是插拔式后端。内置一个 SQLite-backed 的简单记忆实现，同时支持扩展外部 provider。

#### 接口定义

```python
class MemoryProvider(ABC):
    """记忆提供者抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def prefetch(self, query: str) -> str:
        """根据用户输入召回相关记忆"""

    @abstractmethod
    def sync_turn(self, user_msg: str, assistant_response: str) -> None:
        """写入记忆（对话后可持久化的信息）"""

    @abstractmethod
    def build_system_prompt_block(self) -> str:
        """返回系统提示词中的记忆相关段落"""

    def shutdown(self) -> None: ...
```

#### MemoryManager

```python
class MemoryManager:
    """记忆管理器——编排多 provider"""

    def __init__(self):
        self._providers: list[MemoryProvider] = []
        self._prefetch_cache = ""

    def add_provider(self, provider: MemoryProvider) -> bool: ...

    def prefetch_all(self, query: str) -> str:
        """全局预取（结果缓存在当前 turn）"""
        ...

    def build_system_prompt(self) -> str:
        """收集所有 provider 的系统提示段落"""
        ...

    def sync_all(self, user_msg: str, assistant_response: str) -> None:
        """全局同步"""
        ...

    def on_turn_start(self, turn_num: int, message: str) -> None:
        """回合开始钩子"""
        ...
```

#### 内置 SQLite 记忆提供者

```python
class SQLiteMemoryProvider(MemoryProvider):
    """基于 SQLite 的轻量记忆实现"""

    name = "sqlite"

    def __init__(self, db_path: str = "~/.agent/memory.db"):
        self.db_path = Path(db_path).expanduser()
        self._init_db()

    def _init_db(self):
        """创建 memory 表"""
        # CREATE TABLE memories (
        #   id INTEGER PRIMARY KEY AUTOINCREMENT,
        #   key TEXT UNIQUE,
        #   content TEXT,
        #   created_at TIMESTAMP,
        #   updated_at TIMESTAMP
        # )

    def prefetch(self, query: str) -> str:
        """FTS5 全文检索记忆"""
        ...

    def sync_turn(self, user_msg, assistant_response):
        """提取关键信息并持久化"""
        ...

    def build_system_prompt_block(self) -> str:
        """返回格式化的记忆上下文块"""
        ...

    def write(self, target: str, content: str) -> None:
        """写入记忆条目（对应 memory 工具的 write）"""
        ...
```

#### 记忆注入时机

```
回合开始前:
  MemoryManager.prefetch_all(query)
    ↓
记忆上下文注入到 user message（不注入 system prompt）
    ↓
回合结束后:
  MemoryManager.sync_all(user_msg, assistant_response)
```

#### 从 hermes-agent 复用的代码

- **`agent/memory_provider.py`** (291行) — MemoryProvider ABC，直接复用
- **`agent/memory_manager.py`** (640行) — MemoryManager 核心编排逻辑，精简后约 150 行
- **`agent/think_scrubber.py`** — `StreamingContextScrubber` 状态机（流式文本过滤记忆标签）
- 关键的**设计原则**：记忆上下文注入 user message 而非 system prompt（保持系统提示词缓存稳定）

---

### 2.5 State：状态持久化 (mini-state)

**对应 hermes-agent 文件**: `hermes_state.py`

#### 接口设计

```python
class StateStore:
    """会话状态持久化存储（SQLite + FTS5）"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or "~/.agent/state.db"
        self._conn: sqlite3.Connection | None = None
        self._init()

    # ── 会话生命周期 ──
    def create_session(self, source: str = "cli", model: str = "") -> str:
        """创建新会话，返回 session_id"""

    def end_session(self, session_id: str): ...

    def get_session(self, session_id: str) -> dict: ...

    # ── 消息管理 ──
    def append_message(self, session_id: str, msg: dict): ...

    def get_messages(self, session_id: str) -> list: ...

    def replace_messages(self, session_id: str, messages: list): ...

    # ── FTS5 搜索 ──
    def search_messages(self, query: str, limit: int = 5) -> list: ...

    # ── Token 统计 ──
    def update_token_counts(self, session_id: str, **counts): ...

    # ── 辅助 ──
    def list_recent_sessions(self, limit: int = 10) -> list: ...

    def set_session_title(self, session_id: str, title: str): ...
```

#### Schema 设计

```sql
-- 会话表
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    model TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    platform TEXT DEFAULT '',
    parent_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    estimated_cost REAL DEFAULT 0,
    api_call_count INTEGER DEFAULT 0
);

-- 消息表（带 FTS5）
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,       -- JSON
    tool_call_id TEXT,
    name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- FTS5 全文搜索
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content, content=messages, content_rowid=id
);
```

#### 从 hermes-agent 复用的代码

- **`hermes_state.py`** (3516行) — 核心 API（create_session、append_message、search_messages），精简到 ~500 行
- **WAL 模式 + NFS fallback** — `_try_wal_checkpoint` + `_WAL_INCOMPAT_MARKERS`
- **FTS5 全文搜索** — 消息的跨会话全文检索

---

### 2.6 Prompt：提示词工程 (mini-prompt)

**对应 hermes-agent 文件**: `agent/system_prompt.py`

#### 设计思想

三层级组装 + 缓存复用：

| 层级 | 内容 | 变更频率 | 说明 |
|------|------|----------|------|
| **Stable** | Agent 身份定义、工具使用指南、平台提示 | 几乎不变 | 全局 identity + tool guidance |
| **Context** | 用户提供的 system_message、项目上下文文件 | 每轮可能变 | 用户传入或自动发现的上下文 |
| **Volatile** | 记忆快照、用户 profile、时间戳 | 每轮都变 | 运行时信息 |

#### 系统提示词构建器

```python
class SystemPromptBuilder:
    """系统提示词构建器（缓存复用）"""

    def __init__(self, agent):
        self.agent = agent
        self._cached: str | None = None

    def build(self, system_message: str | None = None) -> str:
        """构建或返回缓存的系统提示词"""
        if self._cached is not None:
            return self._cached

        parts = []

        # [Stable] 身份定义
        parts.append(self._build_identity_block())

        # [Stable] 工具使用指南
        parts.append(self._build_tool_guidance())

        # [Context] 用户提供的 system_message
        if system_message:
            parts.append(system_message)

        # [Volatile] 记忆快照（由 MemoryManager 生成）
        if self.agent.memory:
            mem_block = self.agent.memory.build_system_prompt()
            if mem_block:
                parts.append(mem_block)

        # [Volatile] 时间戳 / 会话信息
        parts.append(f"Current time: {datetime.now().isoformat()}")

        self._cached = "\n\n".join(parts)
        return self._cached

    def invalidate_cache(self):
        """缓存失效（上下文压缩后调用）"""
        self._cached = None
```

#### 从 hermes-agent 复用的代码

- **`agent/system_prompt.py`** (407行) — 三层级设计理念、系统提示词一次构建多次复用
- **`agent/prompt_builder.py` 中的常量** — TOOL_USE_ENFORCEMENT_GUIDANCE、SKILLS_GUIDANCE、DEFAULT_AGENT_IDENTITY 等可直接引用
- **缓存在 `_cached_system_prompt` 上** — 保持 prefix cache warm 的完整方案

---

## 3. 核心流程详解：用户输入→Action 决策

### 3.1 run_conversation 完整流程图

```
用户输入 → run_conversation(agent, user_message)
╔═══════════════════════════════════════════════════════════════╗
║  Phase 1: 入口初始化                                          ║
╠═══════════════════════════════════════════════════════════════╣
║ ① 确保 session 存在 (state.create_session)                    ║
║ ② 消毒异常字符 (用户粘贴代理字符)                               ║
║ ③ 构建/缓存 system prompt (仅首次构建)                         ║
║ ④ 预检上下文压缩 (如果历史已超限)                               ║
║ ⑤ 外部记忆预取 (prefetch_all)                                 ║
║ ⑥ 构建 user message → append 到 messages                      ║
╚═══════════════════════════════════════════════════════════════╝
                           │
                           ▼
╔═══════════════════════════════════════════════════════════════╗
║  Phase 2: 主循环                                              ║
╠═══════════════════════════════════════════════════════════════╣
║ while (api_call_count < max_iterations AND budget > 0):       ║
╠═══════════════════════════════════════════════════════════════╣
║                                                               ║
║  ┌─ Step 1: 中断检测 ──────────────────────────────────────┐  ║
║  │  用户发新消息或 /stop → break (返回 partial)              │  ║
║  └──────────────────────────────────────────────────────────┘  ║
║                                                               ║
║  ┌─ Step 2: 准备 API 消息 ─────────────────────────────────┐  ║
║  │  ① 记忆上下文注入 → user message                          │  ║
║  │  ② 消息消毒 (代理字符、角色交替修复)                        │  ║
║  │  ③ 构建 system + prefill (可选)                          │  ║
║  │  ④ 追加 tool schemas                                     │  ║
║  └──────────────────────────────────────────────────────────┘  ║
║                                                               ║
║  ┌─ Step 3: API 调用 ──────────────────────────────────────┐  ║
║  │  transport.build_kwargs(model, messages, tools)           │  ║
║  │  SDK client.chat.completions.create(**kwargs)             │  ║
║  │  优先流式 (健康检查友好)                                    │  ║
║  └──────────────────────────────────────────────────────────┘  ║
║                                                               ║
║  ┌─ Step 4: 响应验证 ──────────────────────────────────────┐  ║
║  │  transport.validate_response(response)                    │  ║
║  │  无效 → retry_count++                                     │  ║
║  │    ├── 有回退提供商 → 激活 fallback, continue             │  ║
║  │    ├── 超重试上限 → 返回失败                               │  ║
║  │    └── jittered_backoff → retry sleep → continue          │  ║
║  └──────────────────────────────────────────────────────────┘  ║
║                                                               ║
║  ┌─ Step 5: finish_reason 提取 ────────────────────────────┐  ║
║  │  transport.normalize_response(response).finish_reason    │  ║
║  │  → "stop" / "length" / "tool_calls"                     │  ║
║  └──────────────────────────────────────────────────────────┘  ║
║                                                               ║
║  ┌─ Step 6: finish_reason == "length" ─────────────────────┐  ║
║  │  ① 思考预算耗尽 → 友好提示 (有 think 标签但无内容)        │  ║
║  │  ② 有文本 → continuation 请求 (最多 3 次)                │  ║
║  │  ③ 有 tool_calls → retry (1 次)                        │  ║
║  │  ④ 全空 → rollback 到上一个完整 turn                    │  ║
║  └──────────────────────────────────────────────────────────┘  ║
║                                                               ║
║  ┌─ Step 7: 核心决策分支 ──────────────────────────────────┐  ║
║  │                                                           │  ║
║  │  if assistant_message.tool_calls:                         │  ║
║  │  ┌─ Branch A: 执行工具 ─────────────────────────────────┐ │  ║
║  │  │  ① 验证工具名称 → 无效则注入错误让模型自修复 (3次)    │ │  ║
║  │  │  ② 验证 JSON 参数 → 截断/格式错误各处理              │ │  ║
║  │  │  ③ 构建 assistant_msg → append 到 messages          │ │  ║
║  │  │  ④ ToolExecutor.execute(tool_calls, registry)        │ │  ║
║  │  │     ├── 读操作并行 / 写操作串行                      │ │  ║
║  │  │     └── 结果追加到 messages (role="tool")            │ │  ║
║  │  │  ⑤ 上下文压缩检查 → 需要时压缩                       │ │  ║
║  │  │  ⑥ continue (回到 while 顶部)                       │ │  ║
║  │  └─────────────────────────────────────────────────────┘ │  ║
║  │                                                           │  ║
║  │  else:                                                    │  ║
║  │  ┌─ Branch B: 最终响应 ─────────────────────────────────┐ │  ║
║  │  │  ① 纯思考块检查 (无可见内容)                          │ │  ║
║  │  │  ② 空响应分级恢复:                                   │ │  ║
║  │  │     ├── 前一轮 housekeeping 内容回退                  │ │  ║
║  │  │     ├── thinking_prefill 注入 (3 次)                 │ │  ║
║  │  │     └── fallback 消息                                │ │  ║
║  │  │  ③ break (退出循环)                                 │ │  ║
║  │  └─────────────────────────────────────────────────────┘ │  ║
║  └──────────────────────────────────────────────────────────┘  ║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
                           │
                           ▼
╔═══════════════════════════════════════════════════════════════╗
║  Phase 3: 收尾持久化                                          ║
╠═══════════════════════════════════════════════════════════════╣
║ ① 预算耗尽检查 → 请求模型总结                                  ║
║ ② 清理任务资源                                                ║
║ ③ 删除内部重试标记消息 (不持久化)                               ║
║ ④ 持久化会话到 SQLite                                         ║
║ ⑤ 记忆同步 (sync_all)                                         ║
║ ⑥ 返回 {final_response, messages, api_calls, completed, ...}  ║
╚═══════════════════════════════════════════════════════════════╝
```

### 3.2 错误恢复决策树

```
API 错误发生
│
├── UnicodeEncodeError (代理字符) → 消毒后重试 (2次)
├── UnicodeEncodeError (ASCII编码) → 强制 ASCII 模式
├── 图片被拒 (error body 匹配) → 移除图片，标记 disable_vision
├── 上下文超限 → 压缩上下文后重试
├── 429 速率限制 → 激活 fallback / backoff 重试
├── 401 认证错误 → 按 provider 特定逻辑恢复
├── 524/504 超时 → 激活 fallback
├── 空响应 (无内容+无tool_calls) → 3 级空响应恢复
└── 其他 → jittered backoff → max_retries → fallback → 失败返回
```

### 3.3 空响应恢复的 3 级阶梯

```
模型返回 content="" + 无 tool_calls
│
├── 第 1 级: 前一轮 housekeeping 内容回退
│   └── 如"您客气了" + memory.save → 直接返回已推送内容
│
├── 第 2 级: thinking_prefill 注入 (最多 3 次)
│   └── 在消息末尾追加 "Please continue your response."
│   └── 检查上次预填充是否触发了思考
│
└── 第 3 级: 最终 fallback
    └── 返回 "I apologize, but I'm having trouble generating a response."
```

---

## 4. 代码复用策略：哪些直接拿，哪些改，哪些重写

| 组件 | hermes-agent 文件 | 策略 | 说明 |
|------|-------------------|------|------|
| **IterationBudget** | `agent/iteration_budget.py` (62行) | **直接复制** | 线程安全计数器，完全自包含 |
| **ToolRegistry** | `tools/registry.py` (589行) | **精简后复用** (~200行) | 去掉 MCP 刷新、webhook 等不相关功能 |
| **ToolEntry** | `tools/registry.py` 中的类 | **直接复制** | 数据结构 pure data，无外部依赖 |
| **Transport ABC** | `agent/transports/base.py` (89行) | **直接复制** | 抽象基类，零外部依赖 |
| **NormalizedResponse** | `agent/transports/types.py` (162行) | **直接复制** | Dataclass 定义 |
| **ChatCompletionsTransport** | `agent/transports/chat_completions.py` (650行) | **精简版** | 去掉 Gemini/Moonshot/Developer Role 等特化逻辑 |
| **MemoryProvider ABC** | `agent/memory_provider.py` (291行) | **直接复制** | 扩展点设计优秀，无需改动 |
| **MemoryManager** | `agent/memory_manager.py` (640行) | **精简后复用** (~150行) | 去掉 provider 排序、地域区分等复杂逻辑 |
| **SystemPromptBuilder** | `agent/system_prompt.py` (407行) | **参考设计理念重写** | 三层级结构保留，内容自定 |
| **StateStore** | `hermes_state.py` (3516行) | **核心 API 参考重写** (~500行) | 仅保留会话/消息/FTS5 核心路径 |
| **Conversation Loop** | `agent/conversation_loop.py` (4611行) | **核心逻辑参考** | 保留决策状态机，去掉 50+ provider 特化 |
| **Tool 验证逻辑** | `run_agent.py` 中散布 | **提取为独立模块** | 工具名修复、JSON 验证、护栏 |
| **Tool 并行化决策** | `agent/tool_dispatch_helpers.py` | **直接复制** | 读并行写串行逻辑 |
| **辅助客户端** | `agent/auxiliary_client.py` (5629行) | **提取核心** | 只取 `call_llm()` 函数 ≈ 200 行 |
| **Prompt 常量** | `agent/prompt_builder.py` | **选择性引用** | DEFAULT_AGENT_IDENTITY、TOOL_USE_ENFORCEMENT_GUIDANCE |

### 不能直接复用的代码（需重写）

| 模块 | 原因 |
|------|------|
| `cli.py` (11k行) | CLI 架构耦合太紧，需要自定义 |
| `hermes_cli/` 全部 | 插件系统、皮肤引擎、web server 等 |
| `gateway/` 全部 | 多消息平台适配（DIY 不需要） |
| `plugins/` 全部 | 插件加载器依赖 hermes_cli.config |
| `tools/environments/*.py` | Docker/SSH/Modal 等远端执行环境 |
| `tools/delegate_tool.py` | 子任务委派逻辑（DIY 可选） |
| `tools/cronjob.py` | 定时任务调度（DIY 可选） |
| Agent 身份定义 | SOUL.md、自我改进循环等 |
| 多 provider 路由 + fallback 链 | 按需简化 |

---

## 5. 逐个功能对照：hermes-agent 能力映射表

| hermes-agent 功能 | DIY Agent 实现 | 说明 |
|---|---|---|
| **用户输入处理** | `run_conversation(user_message, ...)` | 消毒→构建→注入上下文 |
| **LLM 集成** | `TransportFactory.create()` + `BaseTransport` | OpenAI Compatible / Anthropic |
| **多 Provider 回退** | `_fallback_chain` 列表 | 简化为 provider 列表，无自动探测 |
| **工具调用执行** | `ToolRegistry.dispatch()` + `ToolExecutor` | 串行/并行自判定 |
| **工具自注册** | `tools/*.py` 文件 + import 自动发现 | 完全复用 hermes 模式 |
| **工具集 (Toolsets)** | `_HERMES_CORE_TOOLS` 式分组 | 按场景启用/禁用 |
| **会话持久化** | `StateStore` (SQLite + FTS5) | 会话创建→消息追加→搜索 |
| **会话搜索** | `StateStore.search_messages()` | FTS5 全文检索 |
| **记忆管理** | `MemoryManager` + `MemoryProvider` | 内置 SQLite + 外部 provider |
| **system prompt 构建** | `SystemPromptBuilder` (三层级缓存) | Stable/Context/Volatile |
| **上下文压缩** | `ContextCompressor` (精简版) | 中间轮次摘要，保护首尾 |
| **空响应恢复** | 3 级阶梯 (回退→prefill→fallback) | 完全复用 hermes 模式 |
| **截断处理** | continuation (3次) + rollback | 完全复用 hermes 模式 |
| **错误分类恢复** | 分类表 + jittered backoff | 简化为常见类型 |
| **流式 API** | `_interruptible_streaming_api_call()` | 内置健康检查 |
| **护栏 (Guardrails)** | 可选模块 (`ToolCallGuardrail`) | 工具调用前拦截 |
| **多平台网关** | ❌ 不实现 | DIY 场景不需要 |
| **TUI/UI** | ❌ 不实现 | 可扩展 CLI 或 API Server |
| **插件系统** | ❌ 不实现 | 直接 import 代替 |
| **定时任务** | ❌ 不实现 | 可外部 cron 包装 |
| **子任务委派** | ❌ 不实现 | 可扩展 `delegate_task` 工具 |
| **Kanban** | ❌ 不实现 | 多 agent 协作场景 |
| **图片/视觉** | 可选扩展 | 通过 transport 支持 |
| **成本跟踪** | 简单计数 | estimate_usage_cost() 简化版 |

---

## 6. 项目启动指南

### 6.1 目录结构

```
my-agent/
├── agent/
│   ├── __init__.py
│   ├── core.py                  # Agent 核心类
│   ├── conversation_loop.py     # 对话主循环（从 agent/conversation_loop.py 精简）
│   ├── agent_init.py            # 惰性初始化（从 agent/agent_init.py 精简）
│   ├── agent_runtime_helpers.py # 运行时辅助函数
│   ├── iteration_budget.py      # 迭代预算（直接复制 agent/iteration_budget.py）
│   ├── context_compressor.py    # 上下文压缩（精简版）
│   ├── think_scrubber.py        # StreamingContextScrubber
│   └── transports/
│       ├── __init__.py
│       ├── base.py              # 传输基类（直接复制 agent/transports/base.py）
│       ├── types.py             # 标准响应类型（直接复制 agent/transports/types.py）
│       └── chat_completions.py  # OpenAI 兼容传输层
├── tools/
│   ├── __init__.py
│   ├── registry.py             # 工具注册表（精简版 tools/registry.py）
│   ├── web_search.py           # 示例工具
│   ├── read_file.py            # 示例工具
│   └── write_file.py           # 示例工具
├── memory/
│   ├── __init__.py
│   ├── base.py                 # MemoryProvider ABC（直接复制 agent/memory_provider.py）
│   ├── manager.py              # MemoryManager（精简版 agent/memory_manager.py）
│   └── sqlite_provider.py      # 内置 SQLite 记忆提供者
├── state/
│   ├── __init__.py
│   └── store.py               # StateStore（精简版 hermes_state.py）
├── prompt/
│   ├── __init__.py
│   ├── builder.py             # SystemPromptBuilder
│   └── identities.py          # Agent 身份定义常量
├── cli/
│   ├── __init__.py
│   └── main.py                # 简单 CLI 入口
├── tests/
│   ├── test_core.py
│   ├── test_loop.py
│   └── test_tools.py
├── config.yaml                # 配置文件
└── requirements.txt           # 依赖清单
```

### 6.2 requirements.txt

```
openai>=1.0.0     # LLM API 客户端
# anthropic>=0.30.0   # Anthropic 可选
```

### 6.3 启动快速示例

```python
from agent.core import Agent

agent = Agent(
    model="gpt-4o",
    provider="openai",
    api_key="sk-...",
    max_iterations=50,
)

result = agent.run_conversation("搜索最近的 AI 新闻并总结")
print(result["final_response"])
```

### 6.4 最小可执行的 3 步路线图

| 阶段 | 里程碑 | 预计工作量 |
|------|--------|-----------|
| **Phase 1** | 基础 Agent 循环可用：输入→LLM 调用→返回文本（无工具） | ~300 行 |
| **Phase 2** | 工具系统 + 状态持久化 + 工具调用循环 | ~800 行 |
| **Phase 3** | 记忆管理 + 上下文压缩 + 错误恢复 | ~500 行 |

---

## 7. 附录：架构决策记录 (ADR)

### ADR-1：使用"薄构造+厚初始化"模式

**状态**：采纳

**背景**：hermes-agent 的 AIAgent 构造函数需要接受 60+ 参数，如果全部在 `__init__` 中处理，构造器本身就会非常庞大且难以测试。

**决策**：`__init__` 只保存 `_raw_config` 字典。所有子系统的实例化在首次调用 `run_conversation()` 时通过 `_init()` 触发。

**后果**：构造 Agent 实例几乎零开销（微秒级），适合网关等需要频繁创建的场景。初始化阶段的错误可以被调用方感知而非在 import 时崩溃。

### ADR-2：记忆上下文注入 user message 而非 system prompt

**状态**：采纳

**出处**：hermes-agent `agent/conversation_loop.py` L910-925

**理由**：系统提示词在会话中只构建并缓存一次（保持 prefix cache warm）。如果记忆上下文进了 system prompt，每次记忆变化都会导致缓存完全失效，token 成本上升 4x。

**后果**：记忆上下文是每轮 API 调用时动态注入的、不持久化的变量，不会出现在 session 持久化中。调试时需要注意日志中看不到记忆内容。

### ADR-3：工具注册采用自注册（Self-registration）模式

**状态**：采纳

**出处**：hermes-agent `tools/registry.py`

**理由**：每个工具文件在 import 时自动调用 `registry.register()` 声明自己。核心注册表只需 `discover_tools()` 导入所有工具模块即可完成发现。新增工具不需要修改任何注册代码。

**后果**：工具之间不能有循环导入。工具按文件名的字母序导入（相对顺序不影响功能）。

### ADR-4：传输层采用 Adapter 模式

**状态**：采纳

**出处**：hermes-agent `agent/transports/base.py`

**理由**：不同 LLM API（OpenAI Chat / Anthropic Messages / Bedrock）的输入输出格式各不相同。Adapter 模式将格式转换隔离在 transport 层，核心循环只使用统一的 `NormalizedResponse`。

**后果**：新增一个 provider 只需写一个 transport adapter（~100 行），不需要改动核心循环。

### ADR-5：优先使用流式 API

**状态**：采纳

**出处**：hermes-agent `agent/conversation_loop.py` L1225-1234

**理由**：流式 API 提供了非流式场景不具备的健康检查机制（90s 停滞检测、60s 读取超时）。没有这些，subagent 等静默模式可能无限挂起。

**后果**：即使没有流式消费者（如无 TTS），也优先走流式。API 调用方需要支持流式模式。

### ADR-6：使用 jittered exponential backoff 而非固定重试间隔

**状态**：采纳

**出处**：hermes-agent 的 jittered_backoff() 函数

**理由**：固定间隔重试在分布式系统中可能导致"惊群效应"（所有客户端同时重试）。加入随机抖动分散重试时间。

**公式**：`min(base * 2^retry + uniform(0, jitter), max_delay)`，其中 `base=5s, jitter=2s, max_delay=120s`。

### ADR-7：空响应不等于失败（多级恢复）

**状态**：采纳

**出处**：hermes-agent 的 _empty_content_retries、_thinking_prefill_retries 等机制

**理由**：模型（特别是推理模型/小模型）在工具调用后经常返回空内容。第一次空响应就放弃会大幅降低可靠性。三级恢复显著提升成功率。

**后果**：系统可能在一个"无声"的回合上花费最多 3 次额外 API 调用。这在成本可接受的场景下收益远大于开销。

### ADR-8：状态存储使用 SQLite WAL 模式

**状态**：采纳

**出处**：hermes-agent `hermes_state.py`

**理由**：SQLite 是单进程嵌入式中最成熟的选择。WAL 模式支持读并发，对 CLI 和 API Server 场景足够。FTS5 提供全文搜索不依赖外部搜索引擎。

**后果**：不适合高并发写入场景（>100 写/秒）。NFS/SMB 等网络文件系统不支持 WAL，需要 fallback 到 DELETE 模式。

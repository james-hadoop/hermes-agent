# run_agent.py — AIAgent 核心组件与调用链路

> 文件路径: `run_agent.py` (约 4589 行)
> 核心类: `AIAgent` — Hermes Agent 的对话循环引擎
> 职责: 管理用户**单轮对话**的完整生命周期，包括系统提示词构建、模型 API 调用、工具执行、重试/回退、会话持久化、外部记忆同步

---

## 文档地图

- [0. 核心理解：意图→Action 的 7 步链路（速览）](#0-核心理解意图action-的-7-步链路易速览)
- [1. 架构概览](#1-架构概览)
- [2. AIAgent 持有的实例属性（8 大类）](#2-aiagent-持有的实例属性8-大类)
- [3. 核心调用链路——单轮对话三阶段](#3-核心调用链路单轮对话三阶段)
- [4. 决策树：如何理解用户意图并决定下一步 Action](#4-决策树如何理解用户意图并决定下一步-action)
- [5. 工具执行模型](#5-工具执行模型)
- [6. 错误恢复策略](#6-错误恢复策略)
- [7. 词表（前向引用模式）](#7-词表前向引用模式)
- [8. 关键设计决策](#8-关键设计决策)

---

## 0. 核心理解：意图→Action 的 7 步链路（速览）

这是理解 `run_agent.py` 最简捷的脉络。整个系统的"理解用户意图→决定下一步动作"不依赖单一函数，而是一个**多级决策状态机**：

| 步骤 | 做了什么 | 核心决策 |
|------|----------|----------|
| **① 输入** | `run_conversation(agent, user_message)` 接收用户输入 | 构建 system prompt + user message，加载记忆/工具 schemas |
| **② API 调用** | 将完整 messages + tool schemas 发送给模型 | **模型自主决定**其响应包含什么内容 |
| **③ 响应验证** | `validate_response()` 前置检查 | 响应本身是否有效？(None/空choices/失败状态) → 无效则立即走 retry+fallback，**不看内容** |
| **④ finish_reason** | 通过 transport adapter 映射为标准值 | `stop` / `length` / `tool_calls`（各 provider 原始值不同） |
| **⑤ 截断处理** | `finish_reason == "length"` 的子决策树 | 思考耗尽→友好提示；有文本→continuation(3次)；有 tool_calls→重试(1次)；全空→回滚 |
| **⑥ 核心分支** | **有无 `tool_calls`？** 是整个系统的终极决策点 | 有→验证→执行→continue；无→空响应分级恢复→break |
| **⑦ 出口** | 持久化、记忆同步、文件变异验证 | 返回 `{final_response, messages, api_calls, completed, failed}` |

### 0.1 两个关键洞察

1. **系统不依赖模型的 `finish_reason` 做唯一决策。** 它只是入口——模型说"我完成了"不代表真的完成。系统会在工具名、JSON 参数、护栏、空内容等多个维度做二次验证。

2. **空响应不等于失败。** 系统通过多级恢复机制（前一轮内容回退 → thinking_prefill 注入 → sentinel 标记 → fallback 消息）**主动引导模型继续回复**，而不是简单地放弃。

---

> 以下各章节逐步展开上述链路的全部细节。

---

## 1. 架构概览

### 1.1 文件结构

```
run_agent.py
├── AIAgent __init__              (约 34-340 行, 薄构造 → 厚初始化)   ← 仅绑定传参
├── agent_init.py 的 init_agent()  (约 ~1200 行, 实际初始化)           ← 赋值 60+ 属性
│
├── AIAgent 方法:
│   ├── chat() / run_conversation()     — 用户入口
│   ├── _execute_tool_calls()           — 工具调度 (串行/并行)
│   ├── _invoke_tool()                  — 工具调用转发 → agent_runtime_helpers.invoke_tool
│   ├── _dispatch_delegate_task()       — delegate_task 专用路由
│   ├── 各种 _handle_xxx_error           — 错误分类恢复
│   ├── 各种 _build_xxx / _sanitize_xxx  — 消抖/修复/消息构造
│   └── 各种 getter/setter               — 会话/状态管理
│
├── 模块级辅助:
│   ├── _StreamErrorEvent           — 异常标记类
│   ├── build_request_headers()     — 请求头构建
│   └── 模块常量 (PARTIAL_STREAM_STUB_ID, MAX_CONTENT_TOOL_NAMES 等)
│
└── 核心逻辑实际位于:
    ├── agent/agent_init.py              — 实际初始化 (~1200 行)
    ├── agent/conversation_loop.py       — 对话循环主体 (~4611 行)
    ├── agent/agent_runtime_helpers.py   — 运行时辅助函数
    ├── agent/tool_dispatch_helpers.py   — 工具并行化决策
    └── agent/transports/               — API 传输层适配器
```

### 1.2 关键设计决策

| 决策 | 说明 |
|------|------|
| **薄构造+厚初始化** | `__init__` 仅保存传参，`init_agent()` 在首次 `chat()` 时才执行完整初始化 |
| **前向引用模式** | 在 `run_agent.py` 末尾 `run_agent_from_config()` 中 `from agent.conversation_loop import run_conversation`，避免循环导入 |
| **惰性导入** | 工具、传送门、错误处理等模块均在运行时按需导入，降低启动负担 |
| **对话循环外移** | v4 重构后将核心循环移至 `agent/conversation_loop.py`，`run_agent.py` 保持为入口和组件定义 |
| **转发模式** | `_invoke_tool` → `agent_runtime_helpers.invoke_tool`；`_execute_tool_calls` → `_execute_tool_calls_sequential/concurrent` |

---

## 2. AIAgent 持有的实例属性（8 大类）

这些属性在 `init_agent()` 中被赋值，服务于整个对话生命周期。

### 2.1 连接与认证

| 属性 | 类型 | 说明 |
|------|------|------|
| `base_url` | str | API 端点地址 |
| `api_key` | str | API 密钥 (可能在运行时被 ASCII 消毒) |
| `provider` | str | 提供商名称 (deepseek, openrouter, nous 等) |
| `api_mode` | str | API 模式: chat_completions / anthropic_messages / bedrock_converse / codex_responses |
| `model` | str | 当前模型名称 |
| `client_kwargs` | dict | OpenAI SDK 客户端初始化参数 |

### 2.2 会话状态

| 属性 | 说明 |
|------|------|
| `session_id` | SQLite 会话 ID |
| `messages` / `_session_messages` | 当前对话消息列表 |
| `conversation_history` | 外部传入的对话历史 (用于继续会话) |
| `_cached_system_prompt` | 系统提示词缓存 (一次构建，多轮复用) |
| `ephemeral_system_prompt` | 临时系统提示词 (不持久化) |
| `prefill_messages` | 预填充消息 (用于引导模型推理，不持久化) |

### 2.3 Token 与成本统计

| 属性 | 说明 |
|------|------|
| `session_prompt_tokens` | 当前会话累计输入 token |
| `session_completion_tokens` | 当前会话累计输出 token |
| `session_total_tokens` | 总消耗 |
| `session_estimated_cost_usd` | 估算成本 |
| `session_cost_status/source` | 成本状态标记 (api_paid / subscription_included / unknown) |

### 2.4 循环控制

| 属性 | 说明 |
|------|------|
| `max_iterations` | 最大工具调用迭代次数 (默认 90) |
| `iteration_budget` | 迭代预算 (含已用/剩余/总量) |
| `api_call_count` | 已发起 API 调用次数 |
| `_interrupt_requested` | 中断标记 (用户发新消息 / /stop 命令) |
| `_execution_thread_id` | 当前执行线程 ID |
| `_budget_grace_call` | 预算耗尽后的"宽限一次"标记 |

### 2.5 回调系统

| 属性 | 说明 |
|------|------|
| `stream_delta_callback` | 流式文本逐段回调 (→ TTS / UI) |
| `stream_progress_callback` | 流式进度回调 |
| `thinking_callback` | 思考状态回调 (TUI spinner) |
| `status_callback` | 状态信息回调 |
| `_stream_consumers` | 流式消费者列表 (display, TTS 等) |

### 2.6 工具系统

| 属性 | 说明 |
|------|------|
| `tools` | 当前启用的工具定义列表 (OpenAI tool schema) |
| `valid_tool_names` | 有效工具名称集合 |
| `_invalid_tool_retries` | 无效工具名重试计数 |
| `_invalid_json_retries` | 无效 JSON 参数重试计数 |
| `_tool_guardrail_halt_decision` | 护栏拦截决策 (执行前拦截) |
| `_parallel_tool_calls` | 是否启用工具并行执行 |

### 2.7 记忆与上下文

| 属性 | 说明 |
|------|------|
| `_memory_manager` | 外部记忆管理器 (honcho/mem0/supermemory 等) |
| `context_compressor` | 上下文压缩器 (消息摘要/裁剪) |
| `compression_enabled` | 是否允许上下文压缩 |
| `_use_prompt_caching` | 是否开启 Anthropic 提示词缓存 |

### 2.8 服务配置

| 属性 | 说明 |
|------|------|
| `_fallback_chain` | 回退提供商链 ([[provider, model], ...]) |
| `_fallback_index` | 当前回退索引 |
| `_api_max_retries` | 最大 API 重试次数 |
| `_disable_streaming` | 是否禁用流式 (Provider 不支持时标记) |
| `_force_ascii_payload` | ASCII-only 载荷模式 (系统编码问题) |

---

## 3. 核心调用链路——单轮对话三阶段

### 3.1 阶段一：入口初始化

```
run_conversation(agent, user_message, ...)            ← conversation_loop.py L351
├── _install_safe_stdio()                              ← 防护破损管道
├── _ensure_db_session()                               ← 确保 SQLite 会话存在
├── set_runtime_main()                                 ← 注册当前 provider/model
├── set_session_context()                              ← 标记日志
├── sanitize_surrogates(user_message)                  ← 消毒异常字符
├── build task_id                                      ← 生成或复用任务 ID
├── 构造 user message → messages.append()
├── build/cache system prompt                          ← _restore_or_build_system_prompt()
│   └── 惰性：首次调用构建，后续复用 _cached_system_prompt
├── preflight 上下文压缩                               ← 检查是否超阈值
│   └── 可能多轮压缩，创建新 session
└── plugin hook: pre_llm_call                          ← 注入插件上下文
```

### 3.2 阶段二：主循环

```
while (api_call_count < max_iterations AND budget > 0) OR grace_call:   ← L761
│
├── 1. 中断检测
│   └── if _interrupt_requested → break (返回 partial)
│
├── 2. 准备 API 消息 (每轮构建)
│   ├── 注入 Pre-API steer (外部干预)
│   ├── _sanitize_tool_call_arguments()      ← 修复损坏的 tool_call
│   ├── _repair_message_sequence()           ← 修复消息角色交替
│   ├── 注入 ephemeral context → user message
│   │   ├── 外部记忆 prefetch
│   │   └── 插件 context
│   ├── 构建 system + prefill_messages
│   ├── 应用 Anthropic prompt caching
│   ├── _sanitize_api_messages()             ← 清理孤立 tool result
│   ├── _drop_thinking_only_and_merge_users()← 删除仅有思考的 turn
│   └── JSON 规范化/字符消毒
│
├── 3. 流式/非流式 API 调用
│   ├── 选择流式: 优先流式 (健康检查友好)
│   ├── 排除: Copilot ACP / Mock 客户端
│   └── _interruptible_streaming_api_call()  ← 流式调用 (含超时检测)
│
├── 4. 响应验证 (validate_response)
│   ├── codex_responses / anthropic / bedrock / chat 各自验证
│   └── 无效 → retry_with_backoff → fallback_promotion
│
├── 5. 提取 finish_reason
│   └── length / stop / tool_calls … (各 api_mode 不同映射)
│
├── 6. finish_reason == "length" — 截断处理
│   ├── 思考预算耗尽 → 返回友好提示
│   ├── 有文本 → 请求 continuation (最多 3 次)
│   ├── 有 tool_calls 但截断 → 重试 (1 次) 或返回 partial
│   └── 无内容 → rollback 到上一个完整 turn
│
├── 7. Token 统计 & 成本跟踪 (成功到达这里)
│
├── 8. ── 决定性分支 ──
│   │
│   ├── Branch A: assistant_message.tool_calls 存在 → 执行工具
│   │   ├── 验证工具名 → 无效则注入错误结果让模型自修复 (3 次)
│   │   ├── 验证 JSON args → 截断/无效各自处理
│   │   ├── 护栏检查 → tool_guardrail_halt_decision
│   │   ├── 构建 assistant_msg + 附加到 messages
│   │   ├── _execute_tool_calls()             ← 核心工具执行
│   │   │   └── _should_parallelize_tool_batch() → 串行或并发
│   │   ├── 护栏后制动检查
│   │   ├── 上下文压缩检查 → 需要时压缩
│   │   ├── _session_messages = messages
│   │   └── continue  ← 回到 while 循环头部
│   │
│   └── Branch B: 无 tool_calls → 最终响应
│       ├── 检查纯思考块 (无实际内容)
│       ├── 部分流恢复 (断流后使用已推送内容)
│       ├── 前一轮 housekeeping 工具内容回退
│       ├── 空响应重试 (最多 3 次 prefilled 重试)
│       └── break ← 退出 while 循环
│
└── 外层 try/except:
    └── Exception → 填充孤立工具调用错误结果
        └── 接近迭代上限 → break
```

### 3.3 阶段三：收尾持久化

```
退出 while 循环后:
│
├── 预算耗尽检查 (final_response is None)
│   └── _handle_max_iterations() → 移除工具，请求模型总结
│
├── 确定 completed 状态 (有 final_response + 未达上限 + 未失败)
├── _save_trajectory()
├── _cleanup_task_resources()                      ← 清理 VM/浏览器
├── _drop_trailing_empty_response_scaffolding()    ← 删除私有重试标记
├── _persist_session(messages, conversation_history) ← 写入 SQLite + JSON 日志
├── 写后记忆同步 (_sync_turn_memory_after_write())
├── 文件变异验证 (_warn_if_failed_file_mutations())
└── return {final_response, messages, api_calls, completed, failed, ...}
```

---

## 4. 决策树：如何理解用户意图并决定下一步 Action

这是整个系统最核心的问题。模型返回的结果并不直接决定下一步，而是经过多层验证和分类后**按表决策**。

### 4.1 全量决策状态机

```
                  模型返回
                     │
                     ▼
              ┌──────────────┐
              │ 响应是否有效？ │ ← validate_response() / api_mode 特定验证
              └──────┬───────┘
                     │
          有效？→ 否  │  是→
            ┌────────┘
            ▼                          ┌──────────────────┐
      retry_count++                    │ finish_reason 提取 │
            │                         └────────┬─────────┘
       ┌────┴────┐                             │
       │ 有回退？ │──是→ 激活 fallback           │
       └────┬────┘         │                    │
            │ 否            ▼                    ▼
       ┌────┴────┐     reset retry       ┌──────────────┐
       │ 超上限？ │     continue          │ finish_reason │
       └────┬────┘          │            │ =="length"?   │
            │ 否            │            └──────┬───────┘
            ▼                │                   │
      jittered_backoff       │     是←──────┴──────→ 否
         retry sleep         │                   │
            continue         │                   ▼
                             │            ┌──────────────┐
                             │            │ 有 tool_calls │
                             │            └──────┬───────┘
                             │                   │
                             │          是←──────┴──────→ 否
                             │                   │
                             │                   ▼
                             │            ┌──────────────────────┐
                             │            │ 验证工具名/JSON 参数  │
                             │            └──────────┬───────────┘
                             │                       │
                             │            ┌──────────┴──────────┐
                             │            │          │          │
                             │            ▼          ▼          ▼
                             │        无效名    无效 JSON  全部有效
                             │            │          │          │
                             │            ▼          ▼          ▼
                             │       注入错误     跳过/注入   护栏检查
                             │       → continue  → continue   ↓
                             │                              执行工具
                             │                               ↓
                             │                          continue
                             │
                             ▼
                     ┌──────────────┐
                     │ 截断处理     │
                     │ finish_reason│
                     │ =="length"   │
                     └──────────────┘
```

### 4.2 分阶段决策详解

#### 阶段 A：API 响应有效性验证

在查看模型输出内容之前，先判断响应**本身是否有效**：

| 检查项 | 条件 | 动作 |
|--------|------|------|
| Response is None | `response is None` | 立即回退/重试 |
| Codex 状态 | `status == "failed"/"cancelled"` | 记录错误 → 回退 |
| Anthropic content | `response.content` 非列表/为空 | 立即回退/重试 |
| OpenAI choices | `response.choices` 不存在/为空 | 立即回退/重试 |
| Bedrock output | 无 output/choices | 立即回退/重试 |

**违反以上任何条件** → 不查看内容，直接进入 retry+fallback 链路。

#### 阶段 B：finish_reason 提取

不同 api_mode 通过 transport adapter 将 finish_reason 映射为标准值：

| 原始值 → 标准值 | api_mode |
|-----------------|----------|
| `stop_reason` → `stop` / `length` | anthropic_messages |
| `stop_reason` → `end_turn` / `max_tokens` / `tool_use` | bedrock_converse |
| `choices[0].finish_reason` → `stop` / `length` / `tool_calls` | chat_completions |
| `status` + `incomplete_details` → `stop` / `length` | codex_responses |

**额外检测**：Ollama/GLM 的"假 stop"被 `_should_treat_stop_as_truncated()` 重标记为 `"length"`。

#### 阶段 C：finish_reason == "length" 的子决策

这是最复杂的子分支，根据**截断前留下了什么**：

```
finish_reason == "length"
│
├── 思考预算耗尽
│   └── 有 <think> 标签、无文本、无 tool_calls
│   └── → 返回"思考预算耗尽"友好提示
│
├── 留有可见文本
│   └── text_continuation_retries < 3
│   └── → 追加 continuation 提示，continue
│   └── text_continuation_retries >= 3
│   └── → 返回 partial (已收集的部分文本)
│
├── 留有 tool_calls
│   └── truncated_tool_call_retries < 1
│   └── → continue (不加任何消息，让模型重试)
│   └── truncated_tool_call_retries >= 1
│   └── → 返回 partial (拒绝执行截断的工具)
│
└── 什么都不剩 (全截断了)
    └── 回滚到上一个完整 assistant turn
    └── → 返回 partial
```

#### 阶段 D：正常响应 → 最终决策点

```
finish_reason == "stop" (或 "tool_calls")
│
├── tool_calls 非空 ───────────────────────────────── (Branch A)
│   ├── 验证工具名是否在 valid_tool_names 内
│   │   ├── 否 → 注入错误信息 → 继续循环 (模型自修复)
│   │   │       └── 3 次无效 → 返回 partial
│   │   └── 是 → 继续
│   ├── 验证 JSON 参数是否可解析
│   │   ├── 否 → 检查是否截断 (参数不以 }/] 结尾)
│   │   │   └── 截断 → 返回 partial
│   │   │   └── 格式错误 → 重试/注入错误
│   │   └── 是 → 继续
│   ├── 护栏检查 (tool_guardrail)
│   │   └── 拦截 → 返回护栏消息
│   ├── delegate_task 调用上限检查 (cap)
│   ├── 去重 tool_calls
│   ├── 执行工具 (_execute_tool_calls → 串行/并行)
│   ├── 上下文压缩检查 (should_compress)
│   └── continue (回到 while 头部，下一轮 API 调用)
│
└── tool_calls 为空 ───────────────────────────────── (Branch B)
    ├── 检查纯思考块 (如 "<think>...</think>")
    │   └── 纯思考 → 空响应重试逻辑
    ├── 部分流恢复
    │   └── 流中断但已推送内容 → 使用已推送内容
    ├── 前一轮 housekeeping 工具内容回退
    │   └── "You're welcome!" + memory.save → 直接返回该内容
    ├── 空响应重试
    │   └── _empty_content_retries < 3 → 注入 prefilled 恢复提示
    │   └── >= 3 → 返回 fallback 用户消息
    └── 正常 → break (退出循环，返回 final_response)
```

### 4.3 空响应恢复机制

模型返回 `content=""` 且无 tool_calls 时，系统不直接失败而是分级尝试：

| 级别 | 尝试 | 说明 |
|------|------|------|
| 1 | 前一轮 housekeeping 工具内容回退 | 如果上一个 turn 是 "您客气了" + memory.save，直接复用 |
| 2 | 注入 thinking_prefill 恢复 | 在消息末尾追加 `"Please continue your response."` (不持久化) |
| 3 | 注入 thinking_prefill 第二次 | 检查上一次预填充是否触发了思考，否则换方式再试 |
| 4 | 使用 `_empty_terminal_sentinel` | 最后一次尝试后放弃 |
| 5 | 返回 fallback 消息 | "I apologize, but I'm having trouble generating a response." |

---

## 5. 工具执行模型

### 5.1 执行策略

```
_execute_tool_calls()
└── _should_parallelize_tool_batch(tool_calls)
    ├── True  → _execute_tool_calls_concurrent()
    │            └── 用 ThreadPoolExecutor 并行执行
    └── False → _execute_tool_calls_sequential()
                 └── 逐个串行执行
```

### 5.2 并行化决策规则

工具并行化决策在 `agent/tool_dispatch_helpers.py` 的 `_should_parallelize_tool_batch()` 中实现：

- **读操作** (web_search, search_files, memory 等) 始终可以并行
- **写操作** (write_file, patch 等) 只在写入路径不重叠时并行
- 混合读+写操作 → 串行（写操作安全优先）
- 工具名相同 → 串行
- 读取型工具可以先行执行

### 5.3 工具调用验证

| 验证层 | 检测 | 处理 |
|--------|------|------|
| 1. 工具名 | 调用名不在 valid_tool_names 中 | 自动修复 (`_repair_tool_call`) → 失败则注入错误 |
| 2. JSON 参数 | JSON 解析失败 | 空字符串→空对象，截断→partial，异常→重试 |
| 3. 护栏 | tool_guardrail 拦截 | 立即停止并返回拦截消息 |
| 4. 去重 | 相同工具/相同参数 | 保留第一个，丢弃后续 |
| 5. 限额 | delegate_task 超额 | 截断至最大并发数 |

### 5.4 工具调用后的链式处理

```
工具执行完成后:
├── 护栏后制动检查 (guardrail_halt)
├── iterate_budget 退还 (仅 execute_code)
├── 上下文压缩检查 (should_compress)
├── _session_messages = messages
└── continue → 下一轮 API 调用
```

---

## 6. 错误恢复策略

系统在多个层级实现了弹性恢复，按优先级降序排列：

### 6.1 重试链

```
Provider API 错误
├── 1. 同一 provider 重试 (jittered backoff, 5s~120s)
│   └── 持续重试直到 max_retries (默认 2)
├── 2. 触发回退 (fallback chain)
│   └── 切换 provider/model，重置重试计数
│   └── 成功后 _restore_primary_runtime() 恢复主 provider
├── 3. 消息压缩 (减小上下文，绕过上下文限制错误)
└── 4. 返回终端错误
```

### 6.2 按错误类型分类

| 错误 | 检测方式 | 恢复动作 |
|------|----------|----------|
| UnicodeEncodeError (代理字符) | surrogate 检测 | 消毒后重试 (最多 2 次) |
| UnicodeEncodeError (ASCII 编码) | 'ascii' codec 检测 | 强制 ASCII 模式，消毒后重试 |
| 图片被拒 | error body 关键词匹配 | 移除所有图片，标记 disable_vision |
| 上下文超限 (Ollama) | 运行时错误 | 返回友好错误 (不可恢复) |
| 上下文超限 (其他) | 错误消息匹配 | 压缩上下文后重试 |
| 429 (速率限制) | status_code=429 | 回退 promotion / backoff |
| 401 (认证错误) | status_code=401 | 按 provider 特定逻辑恢复 |
| 524 (超时) | status_code=524 | 回退 promotion |
| 空响应 (finish_reason=stop, 内容空) | content 检查 | 空响应分级恢复 (3 级) |

### 6.3 Nous Portal 速率限制保护

```python
if agent.provider == "nous":
    nous_rate_limit_remaining()
    if rate_limited:
        _try_activate_fallback()
        if no fallback → 返回错误
```

### 6.4 思考预算耗尽检测

```
思考预算耗尽 = has_think_tags AND no_text AND no_tool_calls
→ 返回"请降低 reasoning effort 或增加 max_tokens"提示
```

---

## 7. 词表（前向引用模式）

`run_agent.py` 通过模块底部的前向引用函数解决循环导入：

```python
# run_agent.py 末尾
def run_agent_from_config(...):
    from agent.conversation_loop import run_conversation  # 延迟导入
    return run_conversation(agent, user_message, ...)
```

所有外部调用通过 `run_agent_from_config` 进入，不直接导入 `AIAgent` 或 `run_conversation`。

---

## 8. 关键设计决策

### 8.1 为什么 System Prompt 只构建一次？

```
首次调用 _restore_or_build_system_prompt → 缓存到 _cached_system_prompt
之后每轮复用时：active_system_prompt = agent._cached_system_prompt
仅在上下文压缩（创建新 session）后重建
```

**目的**：维持 Anthropic 提示词缓存的字节稳定性。每次构建生成不同的 prompt → 缓存失效 → token 成本上升 4 倍。

### 8.2 为什么插件上下文不注入 System Prompt？

```
插件 context = ephemeral → 注入 user message (不持久化)
系统 prompt = 只存 Hermes 内部指令
```

**目的**：不污染缓存键。如果插件 context 进了 system prompt，每次插件数据变化都导致缓存完全失效。

### 8.3 为什么优先流式？

```python
# Always prefer the streaming path — even without stream consumers.
# Streaming gives us fine-grained health checking (90s stale-stream
# detection, 60s read timeout) that the non-streaming path lacks.
```

即使没有流式消费者（subagent/quiet mode），也优先走流式，利用其内置的 90s 停滞检测 + 60s 读取超时。

### 8.4 为什么"薄构造"？

```
__init__: 仅保存参数 (34-340行)
init_agent(): 耗时操作在首次 chat() 时触发
```

避免网关创建 AIAgent 实例时产生不必要的初始化开销（网关每次消息都创建新 agent）。

### 8.5 为什么需要多级空响应恢复？

模型（特别是较小的模型/推理模型）在工具调用后经常返回空内容。系统不在第一次空响应就放弃，而是：

1. 检查是否可回退到之前的"附带工具的内容"
2. 注入 thinking_prefill 提示
3. 重试 prefilled 恢复
4. 最终 fallback

---

## 附录：子模块索引

| 路径 | 职责 | 关键函数 |
|------|------|----------|
| `agent/agent_init.py` | 实际初始化 (~1200 行) | `init_agent()` |
| `agent/conversation_loop.py` | 对话循环主体 (~4611 行) | `run_conversation()` |
| `agent/tool_dispatch_helpers.py` | 工具并行化决策 | `_should_parallelize_tool_batch()` |
| `agent/agent_runtime_helpers.py` | 运行时辅助函数 | `invoke_tool()` |
| `agent/system_prompt.py` | 系统提示词构建 | `build_system_prompt()` |
| `agent/chat_completion_helpers.py` | API kwargs、max_tokens 处理 | `_build_api_kwargs()` |
| `agent/context_compressor.py` | 上下文压缩 | `should_compress()`, `compress()` |
| `agent/file_mutations.py` | 文件变异验证 | `_warn_if_failed_file_mutations()` |
| `agent/transports/*.py` | API 传输层 | `normalize_response()`, `validate_response()` |
| `agent/nous_rate_guard.py` | Nous 速率限制保护 | `nous_rate_limit_remaining()` |
| `agent/auxiliary_client.py` | 辅助推理客户端 | `set_runtime_main()` |

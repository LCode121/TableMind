# 流式代码生成与执行方案设计

## 一、概述

### 1.1 目标
实现代码生成与执行的**同步流式处理**，让 LLM 生成的代码能够边生成边执行，并通过 MCP notification 机制实时推送执行步骤给后端。

### 1.2 核心需求

| 需求 | 说明 |
|------|------|
| 流式代码生成 | LLM 以流式方式输出代码 |
| 分片执行 | 每生成一个完整代码片段就立即放入沙盒执行 |
| 步骤通知 | 通过 notification 将注释/步骤信息发送给后端 |
| 执行队列 | 解决代码片段执行顺序和并发问题 |
| 错误恢复 | 执行出错时打断生成，进行错误修复重试 |
| 重试限制 | 避免无限重试，设置最大重试次数 |

### 1.3 架构概览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    流式代码生成与执行架构                                      │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────┐    Stream     ┌──────────────────────┐    Queue     ┌─────────────┐
│              │  ─────────▶   │                      │  ─────────▶  │             │
│   LLM API    │    Chunks     │  StreamCodeParser    │   Segments   │   Sandbox   │
│              │  ◀─────────   │                      │  ◀─────────  │   Worker    │
└──────────────┘   Interrupt   └──────────────────────┘   Results    └─────────────┘
                                         │
                                         │ Notification
                                         ▼
                               ┌──────────────────────┐
                               │   Backend (SSE)      │
                               │   {key_step, step}   │
                               └──────────────────────┘
```

---

## 二、代码分片策略

### 2.1 分片标识符设计

采用**注释标记**作为代码分片边界，这样既能让 LLM 自然地添加步骤说明，又能作为分片依据。

#### 方案 A：固定格式注释分隔（推荐）

```python
# [STEP] 读取表格数据
import pandas as pd
df = pd.read_csv('/data/file.csv')

# [STEP] 数据清洗
df = df.dropna()
df = df.drop_duplicates()

# [STEP] 统计分析
result = df.describe()
```

**分片规则：**
- 以 `# [STEP]` 开头的注释作为分片边界
- 注释内容作为步骤描述
- 注释到下一个 `# [STEP]` 之间的代码作为一个执行片段

#### 方案 B：语义分片（备选）

```python
# === 步骤1: 读取表格数据 ===
import pandas as pd
df = pd.read_csv('/data/file.csv')
# === END ===

# === 步骤2: 数据清洗 ===
df = df.dropna()
# === END ===
```

#### 方案 C：代码块分片（备选）

按完整的语句块/函数定义作为分片单元，需要更复杂的语法解析。

### 2.2 推荐方案：方案 A

**理由：**
1. 格式简洁，LLM 容易学习和遵循
2. 分片逻辑简单，基于正则匹配即可
3. 注释内容直接作为步骤描述，无需额外解析
4. 向后兼容，普通 Python 执行不受影响

### 2.3 Prompt 改造

在代码生成的 System Prompt 中添加以下约束：

```markdown
## 代码格式要求

生成的代码必须按步骤组织，每个步骤使用以下格式的注释标记：

# [STEP] <步骤描述>
<该步骤的代码>

示例：
# [STEP] 读取数据
import pandas as pd
df = pd.read_csv('/data/sales.csv')

# [STEP] 数据预处理
df['date'] = pd.to_datetime(df['date'])
df = df.dropna(subset=['amount'])

# [STEP] 计算统计指标
result = df.groupby('category')['amount'].agg(['sum', 'mean', 'count'])

# [STEP] 输出结果
result_df = result.reset_index()

注意事项：
1. 每个 [STEP] 标记后必须有简短的步骤描述
2. 相关的代码逻辑应该放在同一个步骤中
3. 步骤划分应该合理，每个步骤完成一个独立的功能
4. 步骤描述应该简洁明了，便于用户理解
```

---

## 三、流式解析器设计

### 3.1 StreamCodeParser 职责

| 功能 | 说明 |
|------|------|
| 流式接收 | 接收 LLM 输出的 token 流 |
| 缓冲累积 | 缓冲未完成的代码片段 |
| 分片检测 | 检测到 `# [STEP]` 时产出上一个完整片段 |
| 步骤提取 | 从注释中提取步骤描述 |
| 中断信号 | 支持从外部打断流式解析 |

### 3.2 解析状态机

```
┌─────────────────────────────────────────────────────────────────┐
│                     StreamCodeParser 状态机                      │
└─────────────────────────────────────────────────────────────────┘

                           ┌─────────────┐
                           │   IDLE      │
                           └──────┬──────┘
                                  │ receive first token
                                  ▼
                           ┌─────────────┐
         ┌─────────────────│  BUFFERING  │◀────────────────┐
         │                 └──────┬──────┘                 │
         │                        │                        │
         │   detect "# [STEP]"    │   continue buffering   │
         │                        │                        │
         ▼                        ▼                        │
  ┌─────────────┐          ┌─────────────┐                │
  │   YIELD     │─────────▶│  BUFFERING  │────────────────┘
  │  (emit seg) │          └─────────────┘
  └─────────────┘
         │
         │ error / interrupt
         ▼
  ┌─────────────┐
  │  INTERRUPTED│
  └─────────────┘
```

### 3.3 核心算法（伪代码）

```
class StreamCodeParser:
    buffer = ""              # 当前累积的代码
    current_step = None      # 当前步骤描述
    segment_queue = []       # 待执行的代码片段队列
    interrupted = False      # 中断标志
    
    function on_token(token):
        if interrupted:
            return
        
        buffer += token
        
        # 检测是否遇到新的 STEP 标记
        if contains_step_marker(buffer):
            # 提取上一段完整代码
            segments = split_by_step_marker(buffer)
            
            for seg in segments[:-1]:  # 除了最后一个（可能不完整）
                step_name, code = parse_segment(seg)
                emit_segment(step_name, code)
            
            # 保留最后一个未完成的片段
            buffer = segments[-1]
    
    function on_stream_end():
        # 流结束时，发送最后一个片段
        if buffer.strip():
            step_name, code = parse_segment(buffer)
            emit_segment(step_name, code)
    
    function interrupt():
        interrupted = true
```

---

## 四、执行队列管理

### 4.1 问题分析

**核心问题**：第一段代码还在执行，第二段代码已经生成完毕，如何处理？

**可能的问题场景**：
1. 代码 A 正在执行，代码 B 已生成 → B 需要等待
2. 代码 A 执行完毕，代码 B 才能执行 → 保持状态一致性
3. 代码 A 执行报错 → 打断生成，修复后重试
4. 沙盒 Worker 状态需要在所有片段间保持

### 4.2 方案对比

| 方案 | 实现位置 | 优点 | 缺点 |
|------|----------|------|------|
| MCP 端队列 | TableMind MCP | 控制力强，可实现复杂逻辑 | 实现复杂 |
| Sandbox 端队列 | Worker 内部 | 简单，天然串行 | 无法感知生成过程 |
| 混合方案 | 两端协作 | 灵活，职责清晰 | 需要良好的协议设计 |

### 4.3 推荐方案：MCP 端队列 + Sandbox 串行保证

```
┌─────────────────────────────────────────────────────────────────┐
│                    执行队列架构                                   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  MCP 端 (StreamExecutionManager)                                │
│                                                                  │
│  ┌────────────┐     ┌──────────────────────────────────┐       │
│  │  Parser    │────▶│  ExecutionQueue                  │       │
│  │            │     │                                   │       │
│  │  流式解析   │     │  [Seg1:pending] [Seg2:pending]   │       │
│  └────────────┘     │  [Seg3:pending]                  │       │
│                     └──────────────────────────────────┘       │
│                                    │                            │
│                                    │ 顺序执行                   │
│                                    ▼                            │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Executor (串行化)                                       │   │
│  │                                                          │   │
│  │  当前执行: Seg1                                          │   │
│  │  等待: Seg2, Seg3                                        │   │
│  │                                                          │   │
│  │  [获取 Lock] → [发送到 Sandbox] → [等待结果] → [释放 Lock]  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                    │                            │
└────────────────────────────────────│────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  Sandbox Worker                                                  │
│                                                                  │
│  IPython Shell (状态保持)                                        │
│  └── 执行代码 → 返回结果/错误                                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.4 队列执行流程

```
┌─────────────────────────────────────────────────────────────────┐
│                    执行队列流程图                                 │
└─────────────────────────────────────────────────────────────────┘

生成器                    执行队列                    Sandbox
  │                         │                           │
  │── Seg1 ────────────────▶│                           │
  │                         │── execute(Seg1) ─────────▶│
  │── Seg2 ────────────────▶│                           │
  │                         │   [Seg2 入队等待]         │
  │── Seg3 ────────────────▶│                           │
  │                         │   [Seg3 入队等待]         │
  │                         │◀──── Seg1 完成 ──────────│
  │                         │                           │
  │                         │── execute(Seg2) ─────────▶│
  │                         │◀──── Seg2 完成 ──────────│
  │                         │                           │
  │                         │── execute(Seg3) ─────────▶│
  │                         │◀──── Seg3 完成 ──────────│
  │                         │                           │
  ▼                         ▼                           ▼
```

### 4.5 关键设计点

#### 1) 异步生产-同步消费模式

- **生产者**（Parser）：异步接收 LLM token，解析出代码片段后放入队列
- **消费者**（Executor）：从队列中顺序取出片段，同步执行，等待结果后再取下一个

#### 2) 执行锁机制

```
ExecutionLock:
  - 同一时刻只有一个代码片段在执行
  - 新片段入队后，检查锁状态
    - 锁空闲：立即获取锁，开始执行
    - 锁被占用：等待上一个执行完成
```

#### 3) Session 绑定

- 整个工具调用过程绑定同一个 Session
- Session 内的所有代码片段共享 IPython 状态
- 工具调用完成后释放 Session

---

## 五、Notification 通知机制

### 5.1 通知格式

按照后端需求方案，notification 格式如下：

```json
{
  "key_step": true,
  "content": "<代码内容>",
  "step": "<步骤描述>"
}
```

### 5.2 通知时机

| 时机 | 发送内容 | key_step |
|------|----------|----------|
| 解析到新 STEP | 步骤描述 | `true` |
| 代码片段开始执行 | 执行状态 | `false` |
| 代码片段执行完成 | 执行结果摘要 | `false` |
| 执行出错 | 错误信息 | `true` |
| 开始错误修复 | 修复说明 | `true` |

### 5.3 通知流程示例

```
时间线                  Notification 内容
  │
  ├─ 解析到 STEP1 ─────▶ {key_step: true, step: "读取表格数据", content: ""}
  │
  ├─ Seg1 执行中 ─────▶ {key_step: false, step: "读取表格数据", content: "import pandas..."}
  │
  ├─ Seg1 执行完成 ────▶ {key_step: false, step: "读取表格数据", content: "✓ 数据读取成功"}
  │
  ├─ 解析到 STEP2 ─────▶ {key_step: true, step: "数据清洗", content: ""}
  │
  ├─ Seg2 执行中 ─────▶ {key_step: false, step: "数据清洗", content: "df = df.dropna()..."}
  │
  ├─ Seg2 执行出错 ────▶ {key_step: true, step: "执行错误", content: "KeyError: 'column_x'"}
  │
  ├─ 开始修复 ─────────▶ {key_step: true, step: "错误修复中", content: "正在分析错误..."}
  │
  ▼
```

### 5.4 MCP Context 通知调用

```python
# 使用 FastMCP 的 Context 发送通知
await context.send_notification(
    method="notifications/step_progress",
    params={
        "key_step": True,
        "content": code_content,
        "step": step_description
    }
)
```

---

## 六、关键问题与解决方案

本节针对流式执行中的几个**关键工程问题**进行分析和设计。

### 6.1 问题一：超时/死循环与错误恢复的冲突

#### 问题分析

| 执行引擎 | 中断死循环方式 | Session 状态 |
|----------|---------------|-------------|
| InteractiveShell (Phase 1) | 杀容器 | **丢失** |
| jupyter_client (Phase 2) | `interrupt_kernel()` | **保留** |

**冲突点**：
- 当 LLM 生成的代码包含 `while True: pass` 等死循环
- 超时机制触发，需要中断执行
- 但 InteractiveShell 架构下，中断 = 杀容器 = Session 丢失
- "从错误点继续"策略**完全失效**

#### 解决方案

**错误类型识别 + 分级恢复策略**：

```
┌─────────────────────────────────────────────────────────────────┐
│                 错误类型识别与恢复策略                            │
└─────────────────────────────────────────────────────────────────┘

                    ┌─────────────┐
                    │  执行错误   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  错误分类   │
                    └──────┬──────┘
                           │
      ┌────────────────────┼────────────────────┐
      │                    │                    │
      ▼                    ▼                    ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ 运行时错误     │  │ 超时错误      │  │ Worker 崩溃   │
│ (KeyError,    │  │ (TimeoutError)│  │ (Container    │
│  ValueError)  │  │               │  │  Crashed)     │
└───────┬───────┘  └───────┬───────┘  └───────┬───────┘
        │                  │                  │
        ▼                  ▼                  ▼
┌───────────────┐  ┌───────────────┐  ┌───────────────┐
│ Session 保留  │  │ Session 销毁  │  │ Session 销毁  │
│               │  │               │  │               │
│ 策略：从错误  │  │ 策略：完全    │  │ 策略：完全    │
│ 点继续        │  │ 重试          │  │ 重试          │
└───────────────┘  └───────────────┘  └───────────────┘
```

**ErrorRecoveryHandler 错误分类逻辑**：

```
ErrorType 枚举：
  - RUNTIME_ERROR      # 运行时错误，Session 保留
  - TIMEOUT_ERROR      # 超时错误，Session 已销毁
  - WORKER_CRASHED     # Worker 崩溃，Session 已销毁
  - SYNTAX_ERROR       # 语法错误，Session 保留

ErrorRecoveryHandler:
  function classify_error(error):
      if error is TimeoutError:
          return TIMEOUT_ERROR, session_destroyed=True
      if error is WorkerCrashError:
          return WORKER_CRASHED, session_destroyed=True
      if error is SyntaxError:
          return SYNTAX_ERROR, session_destroyed=False
      else:
          return RUNTIME_ERROR, session_destroyed=False

  function recover(error, context):
      error_type, session_destroyed = classify_error(error)
      
      if session_destroyed:
          # Session 已销毁，必须完全重试
          return RecoveryStrategy.FULL_RESTART
      else:
          # Session 保留，可以从错误点继续
          return RecoveryStrategy.CONTINUE_FROM_ERROR
```

**恢复策略对比**：

| 策略 | 触发条件 | 行为 |
|------|----------|------|
| CONTINUE_FROM_ERROR | Session 保留 | 利用已有变量，从错误点继续生成 |
| FULL_RESTART | Session 销毁 | 创建新 Session，完全重新生成代码 |

**长期优化**：
- 尽快升级到 `jupyter_client` 架构
- 支持 `interrupt_kernel()` 中断执行
- 在不杀容器的情况下中断死循环，保住上下文

---

### 6.2 问题二：队列清理的时序竞态

#### 问题分析

**场景**：
```
时间线：
  T1: Parser 产出 Seg1, Seg2, Seg3 入队
  T2: Executor 开始执行 Seg1
  T3: Seg1 执行报错，Sandbox 返回错误并清理脏变量
  T4: Executor 收到错误，但如果处理慢了...
  T5: Executor 取出 Seg2 发送给 Sandbox
  T6: Seg2 执行报 NameError（因为依赖 Seg1 的变量，但 Seg1 已回滚）
  
问题：二次报错，浪费 Token 和时间
```

**根因**：异步生产 + 同步消费之间存在**竞态条件**

#### 解决方案：执行控制器 + 原子操作

```
┌─────────────────────────────────────────────────────────────────┐
│                 执行控制器设计                                    │
└─────────────────────────────────────────────────────────────────┘

                    ExecutionController
┌─────────────────────────────────────────────────────────────────┐
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐    │
│  │   Parser     │────▶│   Queue      │────▶│   Executor   │    │
│  └──────────────┘     └──────────────┘     └──────────────┘    │
│         │                    │                    │             │
│         │                    │                    │             │
│         ▼                    ▼                    ▼             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              abort_flag (原子变量)                       │   │
│  │              state_lock (互斥锁)                         │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**关键操作流程**：

```
ExecutionController:
  abort_flag = False
  state_lock = Lock()
  
  # Executor 主循环
  function execute_loop():
      while True:
          with state_lock:
              if abort_flag:
                  queue.clear()  # 清空队列
                  break
              
              segment = queue.pop()
              if segment is None:
                  break
          
          # 执行代码片段
          result = sandbox.execute(segment)
          
          if result.is_error:
              # 立即设置中止标志
              with state_lock:
                  abort_flag = True
              
              # 通知 Parser 停止
              parser.interrupt()
              
              # 触发错误恢复
              handle_error(result.error)
              break
  
  # Parser 入队前检查
  function enqueue(segment):
      with state_lock:
          if abort_flag:
              return False  # 拒绝入队
          queue.push(segment)
          return True
```

**时序保证**：

| 步骤 | 操作 | 锁状态 |
|------|------|--------|
| 1 | Executor 收到错误 | - |
| 2 | 获取 state_lock | 🔒 |
| 3 | 设置 abort_flag = True | 🔒 |
| 4 | 释放 state_lock | - |
| 5 | Parser 尝试入队 Seg2 | - |
| 6 | 获取 state_lock | 🔒 |
| 7 | 检查 abort_flag = True | 🔒 |
| 8 | 拒绝入队，丢弃 Seg2 | 🔒 |

**关键点**：
- `abort_flag` 和队列操作必须在同一个锁的保护下
- Executor 收到错误后**立即**设置标志，不做任何其他操作
- Parser 入队前**必须**检查标志

---

### 6.3 问题三：变量序列化开销优化

#### 问题分析

**场景**：
```
代码片段执行流程：
  Seg1: df = pd.read_csv(...)     → 请求返回 df 预览？
  Seg2: df = df.dropna()          → 请求返回 df 预览？
  Seg3: result = df.describe()    → 请求返回 result 预览？

每次都序列化大 DataFrame 会导致：
  - 大量 JSON 序列化 CPU 开销
  - 网络传输延迟
  - 拖慢"边生成边执行"的节奏
```

#### 解决方案：智能序列化策略

```
┌─────────────────────────────────────────────────────────────────┐
│                 智能序列化决策                                    │
└─────────────────────────────────────────────────────────────────┘

                    ┌─────────────┐
                    │  代码片段   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  是最后一个  │
                    │  片段？      │
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              │                         │
              ▼ Yes                     ▼ No
       ┌─────────────┐          ┌─────────────┐
       │ 需要序列化  │          │  是否需要   │
       │ result_var  │          │  错误恢复？ │
       └─────────────┘          └──────┬──────┘
                                       │
                          ┌────────────┴────────────┐
                          │                         │
                          ▼ Yes                     ▼ No
                   ┌─────────────┐          ┌─────────────┐
                   │ 只请求变量  │          │ 不请求任何  │
                   │ 名称列表    │          │ 序列化      │
                   └─────────────┘          └─────────────┘
```

**序列化策略表**：

| 场景 | result_var | 返回内容 | 开销 |
|------|------------|----------|------|
| 中间片段正常执行 | `null` | 仅 `{success: true}` | 极低 |
| 最后一个片段 | 指定变量名 | 完整序列化结果 | 正常 |
| 错误恢复查询变量 | 特殊标记 | 仅变量名和类型列表 | 低 |
| 调试模式 | 每个都指定 | 完整序列化 | 高（仅调试用） |

**执行请求格式优化**：

```json
// 中间片段 - 不需要返回值
{
    "code": "df = df.dropna()",
    "result_var": null,
    "mode": "execute_only"
}

// 最后一个片段 - 需要返回结果
{
    "code": "result_df = df.groupby('category').sum()",
    "result_var": "result_df",
    "mode": "execute_and_return"
}

// 错误恢复 - 只需要变量列表
{
    "code": null,
    "result_var": null,
    "mode": "list_variables"
}
```

**实现逻辑**：

```
StreamExecutor:
  function execute_segment(segment, is_last):
      if is_last:
          # 最后一个片段，需要返回结果
          request = {
              "code": segment.code,
              "result_var": extract_result_var(segment.code),
              "mode": "execute_and_return"
          }
      else:
          # 中间片段，只需要成功/失败状态
          request = {
              "code": segment.code,
              "result_var": null,
              "mode": "execute_only"
          }
      
      return sandbox.execute(request)
  
  function get_existing_variables():
      # 错误恢复时获取变量列表
      request = {
          "code": null,
          "result_var": null,
          "mode": "list_variables"
      }
      return sandbox.execute(request)
```

**性能对比**：

| 方案 | 5 个片段的序列化次数 | 预估延迟 |
|------|---------------------|----------|
| 每次都序列化 | 5 次 | ~500ms |
| 智能序列化 | 1 次 | ~100ms |
| 优化比例 | - | **5x 提升** |

---

## 七、错误处理与恢复机制

### 7.1 错误处理流程

```
┌─────────────────────────────────────────────────────────────────┐
│                    错误处理流程（结合错误分类）                    │
└─────────────────────────────────────────────────────────────────┘

                    ┌─────────────┐
                    │  代码执行   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  执行成功?  │
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              │                         │
              ▼ Yes                     ▼ No
       ┌─────────────┐          ┌─────────────┐
       │  继续下一段  │          │ 设置abort   │
       └─────────────┘          │ 清空队列    │
                                └──────┬──────┘
                                       │
                                ┌──────▼──────┐
                                │ 错误类型分类 │
                                └──────┬──────┘
                                       │
                      ┌────────────────┼────────────────┐
                      │                │                │
                      ▼                ▼                ▼
               ┌───────────┐    ┌───────────┐    ┌───────────┐
               │ 运行时错误 │    │ 超时错误   │    │ Worker崩溃│
               │ Session保留│    │ Session丢失│    │ Session丢失│
               └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
                     │                │                │
                     ▼                ▼                ▼
               ┌───────────┐    ┌───────────┐    ┌───────────┐
               │从错误点继续│    │ 完全重试   │    │ 完全重试   │
               └───────────┘    └───────────┘    └───────────┘
```

### 7.2 错误恢复策略

#### 策略 A：从错误点继续（推荐）

```
已执行成功的代码：Seg1, Seg2
执行失败的代码：Seg3 (错误)
待执行的代码：Seg4, Seg5 (丢弃)

恢复流程：
1. 打断 LLM 生成流
2. 清空执行队列 (丢弃 Seg4, Seg5)
3. 保留 Seg1, Seg2 的执行状态（IPython 变量保留）
4. 构建错误修复 Prompt：
   - 原始问题
   - 已成功执行的代码 (Seg1, Seg2)
   - 失败的代码 (Seg3)
   - 错误信息
5. 调用 LLM 生成修复后的代码
6. 继续流式解析和执行
```

#### 策略 B：完全重试

重新开始整个分析过程，适用于早期步骤失败的情况。

### 6.3 错误修复 Prompt 模板

```markdown
## 错误修复任务

### 原始问题
{{original_question}}

### 已成功执行的代码
```python
{{executed_code}}
```

当前 IPython 环境中存在的变量：
{{existing_variables}}

### 执行失败的代码
```python
{{failed_code}}
```

### 错误信息
```
{{error_message}}
```

### 任务
请分析错误原因，并生成修复后的代码。

要求：
1. 利用已有的变量，不要重复执行已成功的代码
2. 修复错误并完成原始问题要求的分析
3. 遵循代码格式要求（使用 # [STEP] 标记）
```

### 6.4 重试次数管理

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| max_retry_count | 3 | 最大重试次数 |
| retry_delay | 1s | 重试间隔 |
| total_timeout | 300s | 整体超时时间 |

#### 重试计数逻辑

```
retry_count = 0

while retry_count < max_retry_count:
    try:
        result = execute_with_stream()
        return result
    except ExecutionError as e:
        retry_count += 1
        if retry_count >= max_retry_count:
            raise MaxRetryExceededError(f"达到最大重试次数 {max_retry_count}")
        
        # 执行错误恢复
        new_code = error_recovery(e)
        continue

raise ExecutionFailedError("执行失败")
```

---

## 八、完整执行流程

### 7.1 主流程时序图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    完整执行流程时序图                                         │
└─────────────────────────────────────────────────────────────────────────────┘

 User        MCP Tool        LLM API       StreamParser    ExecQueue     Sandbox      Backend
  │              │              │              │              │              │           │
  │── 调用工具 ──▶│              │              │              │              │           │
  │              │              │              │              │              │           │
  │              │── 创建 Session ─────────────────────────────────────────▶│           │
  │              │◀─────────────── session_id ─────────────────────────────│           │
  │              │              │              │              │              │           │
  │              │── 流式请求 ──▶│              │              │              │           │
  │              │              │              │              │              │           │
  │              │◀── token1 ───│              │              │              │           │
  │              │              │── token1 ───▶│              │              │           │
  │              │◀── token2 ───│              │              │              │           │
  │              │              │── token2 ───▶│              │              │           │
  │              │              │    ...       │              │              │           │
  │              │              │              │              │              │           │
  │              │              │              │── [检测到 STEP1] ──────────────────────▶│
  │              │              │              │              │              │  notify   │
  │              │              │              │── Seg1 ─────▶│              │           │
  │              │              │              │              │── exec ─────▶│           │
  │              │              │              │              │              │           │
  │              │              │              │── [检测到 STEP2] ──────────────────────▶│
  │              │              │              │── Seg2 ─────▶│              │  notify   │
  │              │              │              │              │  [等待Seg1]  │           │
  │              │              │              │              │              │           │
  │              │              │              │              │◀── 完成 ────│           │
  │              │              │              │              │              │           │
  │              │              │              │              │── exec ─────▶│           │
  │              │              │              │              │◀── 完成 ────│           │
  │              │              │              │              │              │           │
  │              │              │◀── stream end ──────────────│              │           │
  │              │              │              │── Seg3 ─────▶│              │           │
  │              │              │              │              │── exec ─────▶│           │
  │              │              │              │              │◀── 完成 ────│           │
  │              │              │              │              │              │           │
  │              │◀────────────────── 最终结果 ──────────────────────────────│           │
  │              │              │              │              │              │           │
  │◀── 返回 ────│              │              │              │              │           │
  │              │              │              │              │              │           │
```

### 7.2 错误恢复时序图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    错误恢复时序图                                            │
└─────────────────────────────────────────────────────────────────────────────┘

 MCP Tool        LLM API       StreamParser    ExecQueue     Sandbox      Backend
    │              │              │              │              │           │
    │    正常流程...                                                        │
    │              │              │              │              │           │
    │              │              │── Seg3 ─────▶│              │           │
    │              │              │              │── exec ─────▶│           │
    │              │              │              │              │           │
    │              │              │              │◀── ERROR ───│           │
    │              │              │              │              │           │
    │              │◀── 打断信号 ────────────────│              │  notify   │
    │              │              │              │              │  (error)  │
    │              │              │◀─ 中断解析 ──│              │           │
    │              │              │              │              │           │
    │              │              │── 清空队列 ──▶│              │           │
    │              │              │              │              │           │
    │──────────────────────────── 查询已有变量 ──────────────────▶│           │
    │◀───────────────────────────── 变量列表 ────────────────────│           │
    │              │              │              │              │           │
    │── 构建修复 Prompt ─────────▶│              │              │  notify   │
    │              │              │              │              │  (retry)  │
    │              │              │              │              │           │
    │◀── 新代码流 ─│              │              │              │           │
    │              │── 继续解析 ──▶│              │              │           │
    │              │              │── 新Seg ────▶│              │           │
    │              │              │              │── exec ─────▶│           │
    │              │              │              │◀── 完成 ────│           │
    │              │              │              │              │           │
```

---

## 九、关键组件设计

### 8.1 组件职责表

| 组件 | 职责 | 位置 |
|------|------|------|
| StreamingCodeGenerator | 流式调用 LLM，输出 token 流 | MCP |
| StreamCodeParser | 解析 token 流，分割代码片段 | MCP |
| ExecutionQueue | 管理待执行代码片段队列 | MCP |
| StreamExecutor | 顺序执行代码片段，处理错误 | MCP |
| NotificationManager | 发送步骤通知给后端 | MCP |
| ErrorRecoveryHandler | 错误分析和恢复 | MCP |
| SandboxManager | 管理沙盒 Session 和 Worker | MCP |
| Worker | 执行代码，保持状态 | Sandbox |

### 8.2 数据结构定义

```
CodeSegment:
  - id: string                 # 片段唯一标识
  - step_name: string          # 步骤名称
  - code: string               # 代码内容
  - status: enum               # pending | executing | completed | failed
  - result: Optional[any]      # 执行结果
  - error: Optional[string]    # 错误信息
  - execution_time: float      # 执行耗时

ExecutionContext:
  - session_id: string         # 沙盒 Session ID
  - segments: List[CodeSegment]  # 所有代码片段
  - executed_code: string      # 已成功执行的代码
  - variables: Dict            # 当前变量列表
  - retry_count: int           # 当前重试次数
  - start_time: datetime       # 开始时间
```

### 8.3 状态管理

```
StreamExecutionState:
  - INITIALIZING      # 初始化，创建 Session
  - GENERATING        # 代码生成中
  - EXECUTING         # 代码执行中
  - ERROR_RECOVERY    # 错误恢复中
  - COMPLETED         # 执行完成
  - FAILED            # 执行失败（超过重试次数）
  - CANCELLED         # 被取消
```

---

## 十、Sandbox 端适配

### 9.1 需要的接口

沙盒端需要提供以下接口支持流式执行：

| 接口 | 说明 |
|------|------|
| `POST /exec` | 执行代码片段 |
| `GET /variables` | 获取当前 Session 的变量列表 |
| `POST /reset_partial` | 清理指定变量（可选） |
| `GET /health` | 健康检查 |

### 9.2 执行接口增强

```json
// 请求
POST /exec
{
    "code": "df = pd.read_csv('/data/file.csv')",
    "result_var": "df",
    "segment_id": "seg_001"  // 新增：片段标识
}

// 响应（SSE 流）
data: <txt>Loading data...</txt>
data: <result>{"success": true, "execution_time": 0.234}</result>
```

### 9.3 变量查询接口

```json
// 请求
GET /variables

// 响应
{
    "variables": {
        "pd": {"type": "module", "repr": "<module 'pandas'>"},
        "df": {"type": "DataFrame", "shape": [1000, 5]},
        "result": {"type": "DataFrame", "shape": [10, 3]}
    }
}
```

---

## 十一、配置参数

### 10.1 流式执行配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| stream_execution_enabled | true | 是否启用流式执行 |
| step_marker | `# [STEP]` | 步骤标记格式 |
| max_segments_queue_size | 10 | 队列最大长度 |
| segment_timeout | 60s | 单片段执行超时 |
| total_timeout | 300s | 整体执行超时 |

### 10.2 错误恢复配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| max_retry_count | 3 | 最大重试次数 |
| retry_strategy | continue | 重试策略：continue/restart |
| error_recovery_prompt_version | v1 | 错误修复 Prompt 版本 |

### 10.3 通知配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| notification_enabled | true | 是否发送通知 |
| notify_on_step_start | true | 步骤开始时通知 |
| notify_on_step_complete | true | 步骤完成时通知 |
| notify_code_content | true | 是否包含代码内容 |

---

## 十二、改进建议

### 11.1 代码分片的改进方案

除了注释标记分片，还可以考虑：

#### 1) 智能分片（基于语法分析）

- 使用 Python AST 解析
- 按完整语句/函数定义分片
- 优点：不依赖 LLM 遵循格式
- 缺点：实现复杂，无法获取步骤描述

#### 2) 双通道输出

让 LLM 同时输出代码和执行计划：

```json
{
  "plan": [
    {"step": 1, "name": "读取数据", "description": "..."},
    {"step": 2, "name": "数据清洗", "description": "..."}
  ],
  "code": "..."
}
```

- 优点：分离关注点，结构清晰
- 缺点：需要特殊的输出格式处理

### 11.2 执行优化

#### 1) 预加载常用库

在 Session 创建时预先导入常用库：
```python
import pandas as pd
import numpy as np
```

#### 2) 代码预检查

在发送到沙盒前进行语法检查，避免明显的语法错误。

#### 3) 并行准备

当代码段执行时，可以并行进行下一段的语法检查和预处理。

### 11.3 错误处理增强

#### 1) 错误分类

```
ErrorType:
  - SYNTAX_ERROR      # 语法错误，重新生成
  - RUNTIME_ERROR     # 运行时错误，需要修复
  - TIMEOUT_ERROR     # 超时，可能需要优化
  - RESOURCE_ERROR    # 资源不足，需要调整
```

#### 2) 智能错误修复

根据错误类型采用不同的修复策略：
- 语法错误：重新生成整段代码
- 运行时错误：分析错误原因，局部修复
- 超时错误：优化代码或分拆执行

### 11.4 用户体验优化

#### 1) 进度展示增强

```json
{
  "key_step": true,
  "step": "数据清洗",
  "content": "处理中...",
  "progress": {
    "current": 2,
    "total": 5,
    "percentage": 40
  }
}
```

#### 2) 可取消执行

支持用户在执行过程中取消操作。

---

## 十三、实施计划

### Phase 1: 基础设施 (1 周)

- [ ] 设计并实现 StreamCodeParser
- [ ] 实现 ExecutionQueue
- [ ] 修改 LLM 调用为流式模式
- [ ] 修改代码生成 Prompt（添加 STEP 标记要求）

### Phase 2: 核心功能 (1 周)

- [ ] 实现 StreamExecutor
- [ ] 集成 SandboxManager
- [ ] 实现 Notification 发送逻辑
- [ ] 端到端流程测试

### Phase 3: 错误处理 (1 周)

- [ ] 实现执行中断机制
- [ ] 实现 ErrorRecoveryHandler
- [ ] 实现错误修复 Prompt 模板
- [ ] 重试次数管理

### Phase 4: 优化和测试 (0.5 周)

- [ ] 性能优化
- [ ] 异常情况测试
- [ ] 配置参数调优
- [ ] 文档完善

---

## 十四、总结

本方案实现了**代码生成与执行的流式同步处理**：

### 核心设计

| 特性 | 实现方式 |
|------|----------|
| 流式生成 | LLM 流式 API + StreamCodeParser |
| 代码分片 | `# [STEP]` 注释标记 |
| 顺序执行 | MCP 端 ExecutionQueue + Lock |
| 步骤通知 | MCP Notification 机制 |
| 错误恢复 | 打断生成 + 错误修复 LLM 重试 |
| 重试控制 | 最大重试次数 + 超时限制 |

### 关键工程问题解决方案

| 问题 | 解决方案 |
|------|----------|
| 超时/死循环导致 Session 丢失 | 错误类型识别，超时时强制完全重试，长期升级 jupyter_client |
| 队列清理时序竞态 | ExecutionController + abort_flag 原子操作 + 互斥锁 |
| 变量序列化开销 | 智能序列化策略：中间片段不序列化，仅最后片段返回结果 |

### 关键优势

- ✅ 边生成边执行，用户体验更流畅
- ✅ 实时步骤通知，可视化执行进度
- ✅ 智能错误恢复，根据错误类型选择恢复策略
- ✅ 队列保证顺序，abort_flag 避免竞态条件
- ✅ 智能序列化，中间步骤零开销
- ✅ 与现有沙盒架构无缝集成

### 架构依赖与升级路径

| Phase | 执行引擎 | 中断方式 | Session 保持 |
|-------|----------|----------|-------------|
| Phase 1 | InteractiveShell | 杀容器 | ❌ 超时丢失 |
| Phase 2 | jupyter_client | interrupt_kernel() | ✅ 完全保持 |

**建议**：Phase 1 快速上线，Phase 2 尽快跟进以获得完整的错误恢复能力。

### 待确认问题

1. 注释标记格式是否需要调整？
2. Notification 的详细格式是否满足后端需求？
3. 是否需要支持执行过程中的用户取消？
4. jupyter_client 升级的优先级如何排期？


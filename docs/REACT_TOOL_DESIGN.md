# TableMind ReAct 工具设计方案

## 一、概述

### 1.1 核心目标

实现一种 **ReAct (Reasoning + Acting)** 形式的表格操作与数据分析工具，具备以下特性：

| 特性 | 说明 |
|------|------|
| 逐段执行 | 生成一段代码，执行一段，实时反馈 |
| 状态保持 | 前序代码执行结果（变量、DataFrame）在 Session 内持久化 |
| 实时纠错 | 错误发生时立即检测，LLM 生成修复代码，重试执行 |
| 步骤通知 | 通过代码注释捕获关键步骤，FastMCP notification 实时推送给后端 |
| 沙盒隔离 | 适配 SANDBOX_FINAL_DESIGN.md 中的 Session + IPython 方案 |

### 1.2 与现有方案的对比

| 维度 | 现有方案 | ReAct 方案 |
|------|---------|-----------|
| 代码生成 | 一次性生成完整代码 | 分步生成，思考-行动交替 |
| 执行方式 | 本地 exec，无状态隔离 | 沙盒 Session，状态保持 |
| 错误处理 | 执行失败后整体重写 | 单步失败立即修复，不影响已成功的步骤 |
| 进度反馈 | progress 百分比 | 步骤级 notification 通知 |
| 安全性 | 本地执行，风险较高 | gVisor 沙盒隔离 |

---

## 二、整体架构

### 2.1 系统架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ReAct 工具执行流程                                   │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  MCP Client (SemiMind)                                                       │
│  │                                                                          │
│  └── 调用 analyze_data / table_operation 工具                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TableMind MCP Server (FastMCP)                                              │
│  │                                                                          │
│  ├── ReActExecutor (核心编排器)                                              │
│  │   ├── 创建/复用 Sandbox Session                                          │
│  │   ├── ReAct 循环：思考 → 生成代码 → 执行 → 观察                           │
│  │   ├── 步骤注释解析 → FastMCP notification 推送                            │
│  │   └── 错误检测 → 修复代码生成 → 重试                                      │
│  │                                                                          │
│  ├── StepNotifier (步骤通知器)                                               │
│  │   ├── 解析代码中的 # @step: xxx 注释                                     │
│  │   └── 调用 context.info() 发送 notification                              │
│  │                                                                          │
│  └── SandboxClient (沙盒客户端)                                              │
│       └── 与 Gateway API 通信                                               │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Sandbox Gateway                                                             │
│  │                                                                          │
│  └── Session Registry → Worker 容器 (IPython Shell)                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 核心组件

| 组件 | 职责 |
|------|------|
| `ReActExecutor` | ReAct 循环编排，管理对话流程 |
| `ConversationManager` | 管理对话历史（System + User + Assistant 消息） |
| `CodeParser` | 解析 LLM 输出中的代码块（`<\|begin_code\|>...<\|end_code\|>`）和 @step 注释 |
| `StepNotifier` | 从 @step 注释提取步骤名，调用 context.info() 发送 notification |
| `SandboxClient` | 封装 Sandbox Gateway API 调用 |

---

## 三、ReAct 执行流程

### 3.1 核心循环

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          ReAct 循环流程                                       │
└─────────────────────────────────────────────────────────────────────────────┘

用户问题
    │
    ▼
┌─────────────────────────────────────────┐
│  1. 初始化阶段                           │
│  ├── 创建 Sandbox Session               │
│  ├── 加载数据到 Session                  │
│  └── 发送 notification: "数据加载完成"   │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  2. 思考阶段 (Reasoning)                 │  ←──────────────────────┐
│  ├── 分析当前状态和目标                   │                        │
│  ├── 确定下一步操作                       │                        │
│  └── 生成单步代码（含 @step 注释）        │                        │
└─────────────────────────────────────────┘                        │
    │                                                              │
    ▼                                                              │
┌─────────────────────────────────────────┐                        │
│  3. 行动阶段 (Acting)                    │                        │
│  ├── 解析代码中的 @step 注释             │                        │
│  ├── 发送 notification 到后端            │                        │
│  └── 调用 Sandbox 执行代码               │                        │
└─────────────────────────────────────────┘                        │
    │                                                              │
    ▼                                                              │
┌─────────────────────────────────────────┐                        │
│  4. 观察阶段 (Observation)               │                        │
│  ├── 接收 SSE 流式输出                   │                        │
│  ├── 解析执行结果                        │                        │
│  └── 判断是否成功                        │                        │
└─────────────────────────────────────────┘                        │
    │                                                              │
    ├── 成功 ─────────────────────┐                                │
    │                             │                                │
    ▼                             ▼                                │
┌───────────────────┐    ┌───────────────────┐                     │
│  5a. 错误处理     │    │  5b. 继续判断     │                     │
│  ├── 重试次数检查 │    │  ├── 任务完成？   │                     │
│  ├── 生成修复代码 │    │  │   ├── 是 → 结束│                     │
│  └── 重试执行     │    │  │   └── 否 ─────────────────────────────┘
└───────────────────┘    └───────────────────┘
    │
    ├── 超过最大重试 → 返回错误
    └── 重试成功 → 继续循环
```

### 3.2 状态机定义

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          执行状态机                                          │
└─────────────────────────────────────────────────────────────────────────────┘

                    ┌──────────────────┐
                    │   INITIALIZING   │
                    │  创建Session/加载 │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌──────────────────┐
            ┌──────→│    THINKING      │←──────┐
            │       │   思考下一步      │       │
            │       └────────┬─────────┘       │
            │                │                 │
            │                ▼                 │
            │       ┌──────────────────┐       │
            │       │    EXECUTING     │       │
            │       │   执行代码       │       │
            │       └────────┬─────────┘       │
            │                │                 │
            │         ┌──────┴──────┐          │
            │         ▼             ▼          │
            │  ┌─────────────┐ ┌─────────────┐ │
            │  │   SUCCESS   │ │   ERROR     │ │
            │  │   执行成功   │ │   执行失败  │ │
            │  └──────┬──────┘ └──────┬──────┘ │
            │         │               │        │
            │         │        ┌──────┴──────┐ │
            │         │        ▼             ▼ │
            │         │ ┌───────────┐ ┌───────────────┐
            │         │ │ RETRYING  │ │ MAX_RETRY_ERR │
            │         │ │ 重试修复  │ │ 超过最大重试   │
            │         │ └─────┬─────┘ └───────────────┘
            │         │       │                │
            │         │       └────────────────│───→ FAILED
            │         │                        │
            │         ▼                        │
            │  ┌─────────────┐                 │
            │  │  完成判断   │                 │
            │  │  是否继续？ │                 │
            │  └──────┬──────┘                 │
            │         │                        │
            │    ┌────┴────┐                   │
            │    ▼         ▼                   │
            └────是    ┌────────┐              │
                       │COMPLETED│             │
                       │ 任务完成 │←────────────┘
                       └─────────┘
```

---

## 四、步骤通知机制

### 4.1 步骤注释规范

在生成的代码中，通过特定格式的注释标记关键步骤。**步骤名称完全由 LLM 动态生成**，不预定义：

```python
# @step: 加载2024年销售数据
df = pd.read_csv('/data/shared/sales_2024.csv')
print(f"数据加载完成，形状: {df.shape}")

# @step: 处理金额列的缺失值
df['amount'] = df['amount'].fillna(0)
df = df.drop_duplicates()
print(f"清洗后数据形状: {df.shape}")

# @step: 按区域汇总销售总额
result = df.groupby('region')['amount'].sum()
print(result)
```

**关键设计：** 步骤名称应具体描述当前操作的实际内容，而非泛泛的类别（如"数据清洗"）。

### 4.2 注释解析与通知发送

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         注释解析与通知发送                                    │
└─────────────────────────────────────────────────────────────────────────────┘

注释格式：
  # @step: <步骤名称>

解析正则：
  r'#\s*@step:\s*(.+?)(?:\n|$)'

发送时机：
  代码执行前，解析到 @step 注释时立即发送

FastMCP 调用：
  await context.info(json.dumps({
      "key_step": True,
      "content": "",
      "step": step_name  # 从代码注释解析出的步骤名
  }))

数据格式：
  {
    "key_step": true,    // 固定为 true
    "content": "",       // 固定为空
    "step": "<LLM生成>"  // 从代码 @step 注释中解析
  }
```

---

## 五、错误处理与重试机制

### 5.1 核心设计理念

**错误处理逻辑内嵌在主 Prompt 的对话流程中**，无需单独的错误修复 Prompt。当代码执行报错时，将错误信息作为用户消息追加到对话历史，LLM 基于完整上下文自动生成修复代码。

### 5.2 错误分类

| 错误类型 | 示例 | 处理策略 |
|---------|------|---------|
| 语法错误 | SyntaxError, IndentationError | 错误信息返回 LLM，生成修复代码 |
| 运行时错误 | KeyError, ValueError, TypeError | 错误信息返回 LLM，生成修复代码 |
| 数据问题 | 列名不存在、数据格式不匹配 | 错误信息返回 LLM，生成修复代码 |
| 资源限制 | MemoryError, TimeoutError | 终止执行，返回错误 |
| 沙盒错误 | Session 不存在、Worker 异常 | 终止执行，返回错误 |

### 5.3 重试策略配置

```yaml
max_step_retry: 3        # 单步最大重试次数
max_total_retry: 5       # 整体最大重试次数（所有步骤累计）
```

### 5.4 重试流程

```
执行代码
    │
    ▼
检查结果 ───成功───→ 将输出追加到对话历史，继续
    │
    失败
    │
    ▼
retry_count < max_step_retry ?
    │
┌───┴───┐
是      否
│       │
▼       ▼
将错误信息作为    返回错误
用户消息追加      终止执行
到对话历史
    │
    ▼
LLM 基于上下文
生成修复代码
    │
    ▼
重新执行
```

### 5.5 错误处理的对话流转

错误信息以特定格式追加到对话历史，作为下一轮用户输入：

```
user:
<|code_error|>
代码执行报错，请修复后重新生成：

KeyError: 'amount'

Traceback:
  File "<stdin>", line 3, in <module>
    result = df.groupby('region')['amount'].sum()
<|code_error|>
```

LLM 看到错误后，基于完整对话上下文（包括数据信息、已执行代码、执行结果）生成修复代码。

---

## 六、沙盒适配设计

### 6.1 集成架构（MCP + 沙盒一体化）

采用**方案A：沙盒与 MCP 集成**，Gateway 作为 MCP 的内部模块运行，通过 DooD 模式管理 Worker 容器。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      MCP + 沙盒一体化架构                                    │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机 (Linux)                                                              │
│                                                                             │
│  Docker Daemon (配置 gVisor: runtime=runsc)                                 │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                    tablemind-network (internal)                      │   │
│  │                                                                      │   │
│  │  ┌───────────────────────────────────────────────────────────────┐  │   │
│  │  │  TableMind MCP 容器 (runtime: runc)                           │  │   │
│  │  │                                                                │  │   │
│  │  │  ┌─────────────────────────────────────────────────────────┐  │  │   │
│  │  │  │  FastMCP Server (:8000)                                 │  │  │   │
│  │  │  │  ├── analyze_data 工具                                  │  │  │   │
│  │  │  │  ├── table_operation 工具                               │  │  │   │
│  │  │  │  └── get_preview_data 工具                              │  │  │   │
│  │  │  └─────────────────────────────────────────────────────────┘  │  │   │
│  │  │                              │                                 │  │   │
│  │  │  ┌─────────────────────────────────────────────────────────┐  │  │   │
│  │  │  │  SandboxManager (内置模块)                              │  │  │   │
│  │  │  │  ├── 挂载 /var/run/docker.sock (DooD 模式)              │  │  │   │
│  │  │  │  ├── 容器池管理 (min: 2, max: 5)                        │  │  │   │
│  │  │  │  ├── Session → Worker 映射                              │  │  │   │
│  │  │  │  └── 代码执行调度                                       │  │  │   │
│  │  │  └─────────────────────────────────────────────────────────┘  │  │   │
│  │  │                              │                                 │  │   │
│  │  │  挂载：                                                        │  │   │
│  │  │  ├── /var/run/docker.sock → docker.sock                       │  │   │
│  │  │  └── /srv/tablemind/data → /data                              │  │   │
│  │  └───────────────────────────────────────────────────────────────┘  │   │
│  │                              │                                       │   │
│  │                              │ Docker API (通过 socket)              │   │
│  │                              ▼                                       │   │
│  │  ┌───────────────────────────────────────────────────────────────┐  │   │
│  │  │  Worker 容器池 (runtime: runsc/gVisor, 动态创建)              │  │   │
│  │  │                                                                │  │   │
│  │  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐            │  │   │
│  │  │  │  Worker-1   │  │  Worker-2   │  │  Worker-N   │            │  │   │
│  │  │  │  Session:A  │  │  Session:B  │  │  (空闲)     │            │  │   │
│  │  │  │ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │            │  │   │
│  │  │  │ │ IPython │ │  │ │ IPython │ │  │ │ IPython │ │ ← gVisor  │  │   │
│  │  │  │ │ Shell   │ │  │ │ Shell   │ │  │ │ Shell   │ │   隔离    │  │   │
│  │  │  │ └─────────┘ │  │ └─────────┘ │  │ └─────────┘ │            │  │   │
│  │  │  │ FastAPI     │  │ FastAPI     │  │ FastAPI     │            │  │   │
│  │  │  │ :9000       │  │ :9000       │  │ :9000       │            │  │   │
│  │  │  └─────────────┘  └─────────────┘  └─────────────┘            │  │   │
│  │  └───────────────────────────────────────────────────────────────┘  │   │
│  │                              │                                       │   │
│  └──────────────────────────────┼───────────────────────────────────────┘   │
│                                 │                                           │
│                        Volume Mount (共享)                                  │
│                                 ▼                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  /srv/tablemind/data                                                 │   │
│  │  ├── shared/     (只读，用户上传的数据文件)                           │   │
│  │  └── sessions/   (读写，会话临时文件)                                 │   │
│  │      └── {session_id}/                                               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 6.2 为什么选择一体化架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      架构选型分析                                            │
└─────────────────────────────────────────────────────────────────────────────┘

方案A（采用）：沙盒与 MCP 集成                 方案B：沙盒单独部署
─────────────────────────────────────────    ─────────────────────────────────
  ┌─────────────────────┐                      ┌─────────────────────┐
  │  MCP 容器           │                      │  MCP 容器           │
  │  ├── FastMCP        │                      │  └── FastMCP        │
  │  └── SandboxManager │                      └─────────────────────┘
  │      (docker.sock)  │                               │ HTTP
  └─────────────────────┘                               ▼
           │ Docker API                        ┌─────────────────────┐
           ▼                                   │  Gateway 容器       │
  ┌─────────────────────┐                      │  └── docker.sock    │
  │  Worker 容器池      │                      └─────────────────────┘
  └─────────────────────┘                               │
                                                        ▼
                                               ┌─────────────────────┐
                                               │  Worker 容器池      │
                                               └─────────────────────┘

优点：                                         优点：
✓ 部署简单（一个 compose）                     ✓ 职责分离更彻底
✓ 减少一层网络调用                             ✓ 可独立扩展
✓ 共享内存可优化小数据传递                     

缺点：                                         缺点：
✗ MCP 容器需要 docker.sock 权限              ✗ 多一层 HTTP 调用
                                              ✗ 部署更复杂

关键结论：
• 两种方案都是 DooD 模式（挂载宿主机 docker.sock）
• 两种方案 DataFrame 都不能直接传递（不同进程空间）
• 方案A 更简单，选择方案A
```

### 6.3 Session 生命周期管理

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      Session 生命周期与工具调用的映射                         │
└─────────────────────────────────────────────────────────────────────────────┘

推荐方案：工具调用级 Session
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  每次工具调用创建新 Session，调用结束销毁                                  │
  │                                                                         │
  │  优点：                                                                  │
  │  • 隔离性好，每次调用独立                                                 │
  │  • 资源及时释放                                                          │
  │  • 实现简单                                                              │
  │                                                                         │
  │  生命周期：                                                              │
  │  analyze_data() 调用                                                    │
  │       │                                                                 │
  │       ├── 1. 从容器池获取空闲 Worker                                     │
  │       ├── 2. 创建 Session 绑定                                          │
  │       ├── 3. ReAct 循环（多次代码执行共享同一 Session）                   │
  │       ├── 4. 工具返回结果                                                │
  │       └── 5. 释放 Worker 回容器池（重置 IPython 状态）                   │
  │                                                                         │
  └─────────────────────────────────────────────────────────────────────────┘
```

### 6.4 SandboxManager 接口设计

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      SandboxManager 接口定义（MCP 内置模块）                  │
└─────────────────────────────────────────────────────────────────────────────┘

class SandboxManager:
    """
    沙盒管理器，作为 MCP 的内置模块
    通过 DooD 模式管理 Worker 容器池
    """
    
    def __init__(self, config: SandboxConfig):
        """
        初始化时连接 Docker daemon，预热容器池
        """
        self.docker_client = docker.from_env()
        self.pool = ContainerPool(config)
        self.session_registry = SessionRegistry()
    
    async def create_session(self) -> str:
        """
        创建 Session
        1. 从容器池获取空闲 Worker
        2. 创建 Session 目录
        3. 绑定 Session → Worker
        """
        
    async def execute(
        self, 
        session_id: str, 
        code: str,
        result_var: str = None
    ) -> AsyncIterator[ExecutionChunk]:
        """
        执行代码，流式返回结果
        
        直接通过容器网络调用 Worker API（无需外部 HTTP）
        
        Yields:
            ExecutionChunk: 
                - type: "text" | "error" | "image" | "result"
                - content: str | dict
        """
        
    async def release_session(self, session_id: str) -> None:
        """
        释放 Session
        1. 重置 Worker 的 IPython 状态
        2. 清理 Session 目录
        3. 归还 Worker 到容器池
        """
        
    async def get_session_info(self, session_id: str) -> dict:
        """获取 Session 信息（用于调试）"""

ExecutionChunk 类型：
  {
    "type": "text",      // print 输出
    "content": "数据加载完成"
  }
  
  {
    "type": "error",     // 错误信息
    "content": "KeyError: 'column_name'"
  }
  
  {
    "type": "image",     // 图片 base64
    "content": "iVBORw0KGgo..."
  }
  
  {
    "type": "result",    // 最终结果
    "content": {
      "success": true,
      "execution_time": 0.234,
      "return_value": {...}
    }
  }
```

### 6.5 数据传递策略（关键设计）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      数据传递策略                                            │
└─────────────────────────────────────────────────────────────────────────────┘

核心原则：不在 MCP 读取 DataFrame 传给沙盒，让沙盒自己读取文件

原因分析：
┌─────────────────────────────────────────────────────────────────────────────┐
│  MCP 进程                          Worker 容器                              │
│  ────────                          ────────────                             │
│  ❌ 错误做法：                                                              │
│  df = pd.read_csv(path)  ──────→  无法传递！不同进程空间                    │
│                                                                             │
│  ✅ 正确做法：                                                              │
│  传递 filepath ────────────────→  df = pd.read_csv(filepath)               │
│  （通过共享卷访问同一文件）                                                  │
└─────────────────────────────────────────────────────────────────────────────┘

数据传递方式：
┌─────────────────────────────────────────────────────────────────────────────┐
│  输入数据：通过共享卷 + 路径传递                                             │
│  ─────────────────────────────────────────────────────────────────────────  │
│                                                                             │
│  1. MCP 接收文件路径参数                                                    │
│     path_or_url = "/data/shared/sales.csv"                                 │
│                                                                             │
│  2. MCP 只做路径验证和转换，不读取文件内容                                   │
│     container_path = convert_to_container_path(path_or_url)                │
│                                                                             │
│  3. 生成的代码在沙盒中读取文件                                              │
│     code = f'''                                                            │
│     import pandas as pd                                                    │
│     df = pd.read_csv('{container_path}')  # 沙盒自己读                     │
│     # ... 分析代码 ...                                                     │
│     '''                                                                    │
│                                                                             │
│  4. 沙盒通过共享卷访问文件                                                  │
│     宿主机: /srv/tablemind/data/shared/sales.csv                           │
│     MCP容器: /data/shared/sales.csv (只读)                                 │
│     Worker容器: /data/shared/sales.csv (只读)                              │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  输出数据：小结果序列化返回，大结果写文件                                    │
│  ─────────────────────────────────────────────────────────────────────────  │
│                                                                             │
│  小结果（< 1MB，如统计结果）：                                               │
│  result = {                                                                │
│      "type": "dataframe",                                                  │
│      "markdown": df.to_markdown(),      # 用于展示                         │
│      "data": df.head(100).to_dict(),    # 用于结构化返回                   │
│      "shape": df.shape                                                     │
│  }                                                                         │
│                                                                             │
│  大结果（> 1MB，如转换后的表格）：                                           │
│  df.to_csv('/data/session/output/result.csv')                              │
│  result = {                                                                │
│      "type": "file",                                                       │
│      "file_path": "/data/session/output/result.csv"                        │
│  }                                                                         │
└─────────────────────────────────────────────────────────────────────────────┘

文件路径映射：
┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机路径                       容器内路径（MCP & Worker 共享）            │
│  ─────────────────────────────────────────────────────────────────────────  │
│  /srv/tablemind/data/shared/     →  /data/shared/     (只读)               │
│  /srv/tablemind/data/sessions/   →  /data/sessions/   (读写)               │
│                                                                             │
│  URL 处理：                                                                 │
│  https://xxx.com/data.csv  →  MCP 下载到 /data/shared/downloads/xxx.csv   │
│                                Worker 读取 /data/shared/downloads/xxx.csv  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 七、Prompt 设计（统一对话式）

本节设计一个**统一的对话式 Prompt**，将代码生成、执行反馈、错误修复整合在同一个对话流程中。

### 7.1 System Prompt（系统提示）

```markdown
# 角色
你是数据分析代码解释器，使用 Python 对表格数据进行分析。
你需要根据用户的需求编写对应的 Python 代码，我会执行你的代码并把结果告知你，你根据执行结果进一步分析或修正。

# 核心规则

## 代码生成规则
1. 根据任务要求，一步一步进行分析
2. 每次只生成一段符合当前分析步骤的 Python 代码
3. 代码必须以 `# @step: <步骤名称>` 注释开头，步骤名称由你根据当前操作自行决定，应具体描述操作内容
4. 代码在 Jupyter Notebook 环境中运行，历史代码已执行，可继续使用先前定义的变量、方法和导入的包
5. 代码结尾需要有输出结果的语句，如 print() 或直接输出变量
6. 你只需要编写代码，不要编造代码输出结果，结果由我执行后告知你

## 代码格式
代码必须以 `<|begin_code|>` 开头，以 `<|end_code|>` 结尾：
<|begin_code|>
# @step: 步骤名称
你的代码
<|end_code|>

## 数据处理注意事项
1. 第一行数据不一定是表头，也可能有多个子表，先整理表格结构确定表头和子表再分析
2. 如果数据布局不规整或存在合并单元格，使用 fillna 等方法先处理
3. 日期相关数值一律使用 datetime 转换，不使用 Excel 日期值
4. 遇到行列位置、单元格地址相关问题时，使用 pd.read_excel 并设置 header=None
5. 最大值、最小值可能同时存在多个，返回数组而非唯一值
6. 使用 matplotlib 画图时，使用 plt.show() 展示

## 错误处理
如果我返回的代码执行结果报错，说明生成的代码有问题，请根据错误信息重新生成修复后的代码。

## 任务完成
当分析任务完成后，在最后一段代码后输出分析结论，并在代码注释中标注 `# @step: 输出分析结论`。

# 输出格式示例

**正常生成代码：**
<思考过程>
<|begin_code|>
# @step: 加载销售数据
import pandas as pd
df = pd.read_csv('/data/shared/sales.csv')
print(f"数据形状: {df.shape}")
print(df.head())
<|end_code|>

**错误修复：**
<分析错误原因>
<|begin_code|>
# @step: 修复列名错误重新统计
# 使用正确的列名 'sales_amount' 替换 'amount'
result = df.groupby('region')['sales_amount'].sum()
print(result)
<|end_code|>

**分析完成：**
<|begin_code|>
# @step: 输出分析结论
print("分析结论：")
print("1. 华东地区销售额最高，占比45%")
print("2. 销售额同比增长12%")
<|end_code|>

根据以上分析，主要结论如下：...
```

### 7.2 对话流转示例

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          完整对话流程示例                                     │
└─────────────────────────────────────────────────────────────────────────────┘

[System]
(上述 System Prompt)

[User - 初始任务]
# 任务
分析各地区的销售额占比，找出销售额最高的地区

# 数据信息
文件路径: /data/shared/sales_2024.csv
列信息:
- region: 地区 (string)
- sales_amount: 销售金额 (float)
- order_date: 订单日期 (datetime)
数据行数: 10000

[Assistant - 第一步]
首先需要加载数据并查看基本结构。

<|begin_code|>
# @step: 加载销售数据并查看结构
import pandas as pd
df = pd.read_csv('/data/shared/sales_2024.csv')
print(f"数据形状: {df.shape}")
print(f"列信息:\n{df.dtypes}")
print(f"\n前5行数据:\n{df.head()}")
<|end_code|>

[User - 执行结果]
<|code_output|>
数据形状: (10000, 3)
列信息:
region          object
sales_amount    float64
order_date      object
dtype: object

前5行数据:
   region  sales_amount  order_date
0  华东        1500.50   2024-01-15
1  华北         980.00   2024-01-16
...
<|code_output|>

[Assistant - 第二步]
数据加载成功，接下来按地区汇总销售额并计算占比。

<|begin_code|>
# @step: 按地区汇总销售额并计算占比
region_sales = df.groupby('region')['sales_amount'].sum().reset_index()
region_sales.columns = ['地区', '销售额']
total = region_sales['销售额'].sum()
region_sales['占比'] = (region_sales['销售额'] / total * 100).round(2)
region_sales = region_sales.sort_values('销售额', ascending=False)
print(region_sales.to_markdown(index=False))
<|end_code|>

[User - 执行错误]
<|code_error|>
代码执行报错，请修复后重新生成：

KeyError: 'sales_amount'

Traceback:
  File "<stdin>", line 2, in <module>
    region_sales = df.groupby('region')['sales_amount'].sum()
<|code_error|>

[Assistant - 修复代码]
错误显示列名 'sales_amount' 不存在，让我先检查实际的列名。

<|begin_code|>
# @step: 检查列名并修正统计
print(f"实际列名: {df.columns.tolist()}")
# 根据实际列名进行汇总
amount_col = [c for c in df.columns if 'amount' in c.lower() or 'sales' in c.lower()][0]
region_sales = df.groupby('region')[amount_col].sum().reset_index()
region_sales.columns = ['地区', '销售额']
total = region_sales['销售额'].sum()
region_sales['占比'] = (region_sales['销售额'] / total * 100).round(2)
region_sales = region_sales.sort_values('销售额', ascending=False)
print(region_sales.to_markdown(index=False))
<|end_code|>

[User - 执行成功]
<|code_output|>
实际列名: ['region', 'amount', 'order_date']

| 地区   | 销售额     | 占比  |
|--------|-----------|-------|
| 华东   | 4500000   | 45.2  |
| 华南   | 2800000   | 28.1  |
| 华北   | 1500000   | 15.0  |
| 其他   | 1200000   | 11.7  |
<|code_output|>

[Assistant - 分析完成]
数据分析完成，输出最终结论。

<|begin_code|>
# @step: 输出分析结论
print("=" * 50)
print("销售额地区分布分析结论")
print("=" * 50)
print(f"1. 销售额最高的地区是：华东，占比 45.2%")
print(f"2. 前两名地区（华东、华南）合计占比超过 73%")
print(f"3. 建议加强其他地区的销售渠道建设")
<|end_code|>

## 分析结论
根据数据分析结果，**华东地区**销售额最高，占总销售额的45.2%...
```

### 7.3 消息格式定义

| 消息类型 | 格式 | 说明 |
|---------|------|------|
| 代码块 | `<\|begin_code\|>...<\|end_code\|>` | LLM 生成的代码 |
| 执行成功 | `<\|code_output\|>...<\|code_output\|>` | 代码执行输出 |
| 执行失败 | `<\|code_error\|>...<\|code_error\|>` | 错误信息 |

### 7.4 完成判断

任务完成的判断方式：
1. **LLM 自主判断**：LLM 在最后一步代码后直接输出分析结论（不再生成新代码）
2. **最大步骤限制**：达到 `max_steps`（默认 10）时强制结束
3. **不可恢复错误**：达到 `max_step_retry` 后仍失败则终止

---

## 八、数据流与接口设计

### 8.1 工具调用数据流

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      analyze_data 工具完整数据流                             │
└─────────────────────────────────────────────────────────────────────────────┘

1. 工具调用开始
   │
   ├── 输入: question, path_or_url
   │
   ▼
2. 初始化
   │
   ├── 创建 Sandbox Session
   ├── 获取数据摘要
   ├── 构建初始对话:
   │   ├── System: System Prompt
   │   └── User: 任务 + 数据信息
   │
   ▼
3. ReAct 循环
   │
   ├── [循环开始]
   │   │
   │   ├── 调用 LLM（传入对话历史）
   │   ├── 解析 LLM 输出:
   │   │   ├── 提取 <|begin_code|>...<|end_code|> 中的代码
   │   │   ├── 解析 # @step: xxx 注释
   │   │   └── 无代码块 → 任务完成
   │   │
   │   ├── 发送 notification: {"key_step": true, "step": "<步骤名>", "content": ""}
   │   ├── 沙盒执行代码
   │   │
   │   ├── 检查执行结果:
   │   │   ├── 成功 → 将输出追加到对话历史 (<|code_output|>)
   │   │   └── 失败 → 将错误追加到对话历史 (<|code_error|>)
   │   │
   │   └── [循环结束条件: 无代码块 / 最大步骤 / 超过重试次数]
   │
   ▼
4. 结果处理
   │
   ├── 提取最终结果
   ├── 销毁 Session
   │
   ▼
5. 返回结果
   │
   └── ToolResult(content=markdown, structured_content=dict)
```

### 8.2 对话历史管理

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         对话历史结构                                         │
└─────────────────────────────────────────────────────────────────────────────┘

conversation = [
    {"role": "system", "content": "<System Prompt>"},
    
    {"role": "user", "content": "# 任务\n分析销售数据\n# 数据信息\n..."},
    
    {"role": "assistant", "content": "...<|begin_code|>\n# @step: 加载数据\n...<|end_code|>"},
    
    {"role": "user", "content": "<|code_output|>\n数据形状: (1000, 5)\n...<|code_output|>"},
    
    {"role": "assistant", "content": "...<|begin_code|>\n# @step: 统计分析\n...<|end_code|>"},
    
    {"role": "user", "content": "<|code_error|>\nKeyError: 'amount'\n...<|code_error|>"},
    
    {"role": "assistant", "content": "...<|begin_code|>\n# @step: 修复列名错误\n...<|end_code|>"},
    
    {"role": "user", "content": "<|code_output|>\n| 地区 | 销售额 |\n...<|code_output|>"},
    
    {"role": "assistant", "content": "分析完成，结论如下：..."}  // 无代码块，任务结束
]
```

### 8.3 错误处理流程

```
执行代码
    │
    ▼
接收执行结果
    │
    ├── 成功 → 格式化为 <|code_output|>...<|code_output|>
    │         追加到对话历史，继续循环
    │
    └── 失败
          │
          ▼
      retry_count < max_step_retry ?
          │
      ┌───┴───┐
      是      否
      │       │
      ▼       ▼
   格式化为   返回错误
   <|code_error|>  终止执行
   追加到对话历史
      │
      ▼
   重新调用 LLM
   LLM 基于上下文
   生成修复代码
```

---

## 九、配置设计

### 9.1 配置项定义

```yaml
# config.yaml 新增配置

react:
  # 执行控制
  max_steps: 10                    # 最大执行步骤数
  max_step_retry: 3                # 单步最大重试次数
  step_timeout: 60                 # 单步执行超时（秒）
  
  # 代码解析
  code_begin_tag: "<|begin_code|>" # 代码开始标记
  code_end_tag: "<|end_code|>"     # 代码结束标记
  step_annotation: "# @step:"      # 步骤注释前缀

# 沙盒配置（一体化架构）
sandbox:
  # 容器池配置
  pool:
    min_size: 2                    # 最小容器数（预热）
    max_size: 5                    # 最大容器数
    scale_up_threshold: 0.8        # 使用率超过 80% 扩容
    scale_down_threshold: 0.3      # 使用率低于 30% 缩容
    health_check_interval: 30      # 健康检查间隔（秒）
  
  # Worker 容器配置
  worker:
    image: tablemind/worker:latest
    runtime: runsc                 # gVisor 隔离
    memory_limit: 2g
    cpu_limit: 1.0
    network: tablemind-network
  
  # Session 配置
  session:
    idle_timeout: 1800             # 空闲超时（秒）
    max_lifetime: 7200             # 最大生存时间（秒）
    execution_timeout: 300         # 单次执行超时（秒）
  
  # 数据目录配置
  data:
    shared_path: /data/shared      # 共享数据目录（只读）
    sessions_path: /data/sessions  # Session 目录（读写）
  
  # 预加载包
  preload_packages:
    - pandas
    - numpy
    - matplotlib
    - scipy
    - statsmodels

notification:
  # 通知格式（固定）
  format:
    key_step: true                 # 固定为 true
    content: ""                    # 固定为空
    step: "<从@step注释解析>"       # 从代码注释中解析
```

**说明：** 步骤名称不再预定义，完全由 LLM 在生成代码时通过 `# @step:` 注释动态决定。

---

## 十、工具接口设计

### 10.1 analyze_data 工具改造

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      analyze_data 工具接口                                   │
└─────────────────────────────────────────────────────────────────────────────┘

入参：
  question: str          # 用户问题
  path_or_url: str       # 数据文件路径或URL

内部流程：
  1. 获取数据访问器，生成数据摘要
  2. 创建 Sandbox Session
  3. 发送 notification: 初始化分析环境
  4. 启动 ReActExecutor
  5. ReAct 循环执行
  6. 收集最终结果
  7. 销毁 Session
  8. 返回 ToolResult

返回：
  ToolResult(
    content=markdown_result,
    structured_content=dict_result
  )

Notification 序列（步骤名由 LLM 动态生成，以下为示例）：
  → {"key_step": true, "step": "加载2024年销售数据", "content": ""}
  → {"key_step": true, "step": "查看数据结构和缺失情况", "content": ""}
  → {"key_step": true, "step": "按地区汇总销售额", "content": ""}
  → {"key_step": true, "step": "输出分析结论", "content": ""}
```

### 10.2 table_operation 工具改造

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      table_operation 工具接口                                │
└─────────────────────────────────────────────────────────────────────────────┘

入参：
  instruction: str       # 操作指令
  input_paths: List[str] # 输入文件路径列表
  output_path: str       # 输出文件路径

内部流程：
  1. 加载所有输入数据，生成数据摘要
  2. 创建 Sandbox Session
  3. 构建初始对话（System Prompt + 用户任务）
  4. 启动 ReActExecutor（表格操作模式）
  5. ReAct 循环执行
  6. 保存结果文件
  7. 销毁 Session
  8. 返回结果路径

返回：
  JSON 字符串: {"file_path": "...", "path_desc": "..."}

Notification 序列（步骤名由 LLM 动态生成，以下为示例）：
  → {"key_step": true, "step": "读取订单表和客户表", "content": ""}
  → {"key_step": true, "step": "按客户ID合并两表", "content": ""}
  → {"key_step": true, "step": "筛选VIP客户订单", "content": ""}
  → {"key_step": true, "step": "保存结果到output.csv", "content": ""}
```

---

## 十一、实现计划

### Phase 1: 沙盒基础设施（优先）

| 任务 | 说明 | 依赖 |
|------|------|------|
| Worker 镜像构建 | IPython 执行引擎 + FastAPI | - |
| SandboxManager | 容器池管理、Session 管理（MCP 内置模块） | Docker SDK |
| 容器池预热 | 启动时创建 min_size 个 Worker | SandboxManager |
| 共享卷配置 | /data/shared 只读 + /data/sessions 读写 | Docker |

### Phase 2: 代码解析与通知

| 任务 | 说明 | 依赖 |
|------|------|------|
| CodeParser | 解析 LLM 输出中的代码块和 @step 注释 | - |
| StepNotifier | 发送 notification 到 FastMCP context | FastMCP |
| 流式注释提取 | LLM 流式输出时实时提取 @step（可选优化） | - |

### Phase 3: ReAct 执行器

| 任务 | 说明 | 依赖 |
|------|------|------|
| ConversationManager | 管理对话历史（System + User + Assistant） | - |
| ReActExecutor | ReAct 循环框架 | SandboxManager |
| 代码执行流程 | 执行代码、收集输出、检测错误 | SandboxManager |
| 重试管理 | 错误时追加到对话、重新调用 LLM | ConversationManager |

### Phase 4: 工具集成

| 任务 | 说明 | 依赖 |
|------|------|------|
| 数据传递改造 | 改为传路径让沙盒自己读取 | 共享卷 |
| analyze_data 改造 | 集成 ReActExecutor | Phase 1-3 |
| table_operation 改造 | 集成 ReActExecutor | Phase 1-3 |
| docker-compose 更新 | 添加 docker.sock 挂载和共享卷 | - |

### Phase 5: 生产就绪

| 任务 | 说明 | 依赖 |
|------|------|------|
| gVisor 集成 | Worker 使用 runsc runtime | - |
| 健康检查 | Worker 健康检查与自动恢复 | - |
| 资源限制 | 内存、CPU、执行超时 | - |
| 集成测试 | 端到端测试 | Phase 4 |

---

## 十二、附录

### A. Notification 示例（步骤名由 LLM 动态生成）

```json
// 步骤名称从代码的 @step 注释中解析，完全由 LLM 决定
{"key_step": true, "content": "", "step": "加载2024年Q1销售数据"}
{"key_step": true, "content": "", "step": "检查数据结构和缺失值"}
{"key_step": true, "content": "", "step": "修复列名错误重新统计"}
{"key_step": true, "content": "", "step": "按地区计算销售额占比"}
{"key_step": true, "content": "", "step": "输出分析结论"}
```

### B. LLM 生成的代码示例

LLM 输出格式（包含思考过程和代码块）：

```
首先需要加载数据并查看基本结构。

<|begin_code|>
# @step: 加载2024年Q1销售数据
import pandas as pd
df = pd.read_csv('/data/shared/sales_2024_q1.csv')
print(f"数据形状: {df.shape}")
print(f"列名: {df.columns.tolist()}")
print(df.head())
<|end_code|>
```

```
数据加载成功，接下来处理金额列的缺失值。

<|begin_code|>
# @step: 处理金额列缺失值
df['amount'] = df['amount'].fillna(0)
print(f"缺失值处理后，amount列非空数量: {df['amount'].notna().sum()}")
<|end_code|>
```

```
现在按地区汇总并计算占比。

<|begin_code|>
# @step: 按地区计算销售额占比
region_stats = df.groupby('region')['amount'].sum().reset_index()
region_stats.columns = ['地区', '销售额']
total = region_stats['销售额'].sum()
region_stats['占比'] = (region_stats['销售额'] / total * 100).round(2).astype(str) + '%'
region_stats = region_stats.sort_values('销售额', ascending=False)
print(region_stats.to_markdown(index=False))
<|end_code|>
```

### C. 错误处理对话流转示例

**执行失败时，系统将错误信息作为用户消息追加：**

```
[User]
<|code_error|>
代码执行报错，请修复后重新生成：

KeyError: 'amount'

Traceback:
  File "<stdin>", line 2, in <module>
    result = df.groupby('region')['amount'].sum()
<|code_error|>
```

**LLM 收到错误后生成修复代码：**

```
错误显示列名 'amount' 不存在。根据之前查看的列信息，正确的列名应该是 'sales_amount'。

<|begin_code|>
# @step: 修复列名错误重新统计
# 使用正确的列名 sales_amount
result = df.groupby('region')['sales_amount'].sum().reset_index()
result.columns = ['地区', '销售额']
print(result.to_markdown(index=False))
<|end_code|>
```

### D. 核心组件职责

| 组件 | 职责 |
|------|------|
| `ReActExecutor` | ReAct 循环编排，管理对话流程 |
| `ConversationManager` | 管理对话历史（System + User + Assistant） |
| `CodeParser` | 解析 LLM 输出中的代码块和 @step 注释 |
| `StepNotifier` | 从 @step 注释提取步骤名，发送 notification |
| `SandboxClient` | 封装 Sandbox Gateway API 调用 |

---

## 十三、竞品分析与优化方案

### 13.1 竞品方案深度解析

#### 竞品输出结构还原

仔细分析竞品输出，前面显示的步骤列表实际上是**从代码注释中提取**出来的：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      竞品输出结构解析                                        │
└─────────────────────────────────────────────────────────────────────────────┘

[用户看到的输出]                         [实际代码中的注释]
─────────────────                         ─────────────────
读取Excel文件                       ←──   # 读取Excel文件
查看数据结构和数值列                 ←──   # 查看数据结构和数值列
选择数值列进行回归分析               ←──   # 选择数值列进行回归分析
选择两个数值列进行简单线性回归示例   ←──   # 选择两个数值列进行简单线性回归示例
...                                       # ...

python                                    python
import pandas as pd                       import pandas as pd
...                                       ...
# 读取Excel文件                           # 读取Excel文件
df = pd.read_excel(...)                   df = pd.read_excel(...)
# 查看数据结构和数值列                    # 查看数据结构和数值列
print(df.shape)                           print(df.shape)
...                                       ...
```

#### 竞品核心机制

**竞品与我们的方案本质相同**：都是通过代码注释来标记步骤，区别在于**提取和展示时机**。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      竞品 vs 我们的方案                                      │
└─────────────────────────────────────────────────────────────────────────────┘

相同点：
  ✓ 都使用代码注释标记步骤
  ✓ 都需要解析注释提取步骤名
  ✓ 都需要发送步骤通知给前端

不同点：
                    竞品                              我们
  注释格式          # 步骤描述                        # @step: 步骤描述
  提取时机          流式生成时实时提取                代码生成完毕后提取
  展示位置          步骤列表在代码上方                notification 实时推送
  执行模式          一次生成完整代码块后执行          逐步生成逐步执行
```

### 13.2 竞品速度快的核心原因：流式注释提取

竞品执行速度快的关键在于**流式注释提取 + 前置展示**：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    流式注释提取机制                                          │
└─────────────────────────────────────────────────────────────────────────────┘

时间线 ─────────────────────────────────────────────────────────────────────→

LLM 流式输出:
  "python\n"
  "import pandas as pd\n"
  "import numpy as np\n"
  "\n"
  "# 读取Excel文件\n"        ─────→ 检测到注释，立即提取 "读取Excel文件"
       │                            并显示在步骤列表中
       ↓
  "df = pd.read_excel(...)\n"
  "\n"
  "# 查看数据结构\n"         ─────→ 检测到注释，立即提取 "查看数据结构"
       │                            追加到步骤列表
       ↓
  "print(df.shape)\n"
  "print(df.dtypes)\n"
  ...
  （代码继续生成）
  ...
  "\n输出\n"                 ─────→ 代码块结束，开始执行

用户视角：
┌─────────────────────────────────────────────────────────────────────────────┐
│  用户几乎在 LLM 开始输出时就能看到步骤列表                                    │
│  不需要等待整个代码块生成完毕                                                │
│  感知延迟大大降低                                                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 流式提取的技术要点

```
1. LLM 以 stream 模式输出

2. 实时逐行解析：
   - 检测到 "# " 开头的行 → 可能是步骤注释
   - 提取注释内容（去掉 # 前缀）
   - 立即展示到步骤列表区域

3. 步骤展示与代码生成并行：
   - 步骤列表先于代码展示
   - 用户可以看到"计划要做什么"
   - 代码在后面逐渐生成出来

4. 代码执行时机：
   - 检测到代码块结束标记
   - 或检测到"输出"等关键词
   - 提交整块代码执行
```

### 13.3 两种方案对比

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          方案对比分析                                        │
└─────────────────────────────────────────────────────────────────────────────┘

                    我们的方案                          竞品方案
                    ──────────                          ────────
注释格式            # @step: 步骤描述                   # 步骤描述
                    (带 @step 前缀)                     (普通注释)

提取时机            代码块生成完毕后解析                 流式生成时实时提取
                    再发送 notification                 边生成边展示

执行粒度            每个 @step 单独执行                  整块代码一起执行
                    (多次 LLM 调用)                     (一次 LLM 调用)

错误恢复            单步失败立即修复                     整块失败重新生成
                    已成功步骤状态保持                   需重新执行全部

用户感知            需等代码生成完才看到步骤             步骤列表先出现
                    延迟较高                            延迟低，体验好

适合场景            复杂分析、需要精确控制               快速探索、简单分析
```

### 13.4 优劣势分析

#### 我们方案的优势

| 优势 | 说明 |
|------|------|
| **精确错误定位** | 单步执行，错误发生在哪一步非常清楚 |
| **最小化重试范围** | 只需重试失败的那一步，已成功步骤不受影响 |
| **状态可追溯** | 每步执行后可以查看当前变量状态 |
| **适合复杂分析** | 多步骤、有依赖关系的任务更可控 |
| **步骤名更规范** | `@step` 前缀明确区分步骤注释和普通注释 |

#### 我们方案的劣势

| 劣势 | 说明 |
|------|------|
| **用户感知延迟高** | 需等代码生成完毕才能看到步骤 |
| **LLM 调用次数多** | 每步都需要完整的 LLM 调用，成本高 |
| **整体耗时长** | 串行执行，等待时间累加 |

#### 竞品方案的优势

| 优势 | 说明 |
|------|------|
| **用户感知延迟低** | 流式提取，步骤列表先显示 |
| **LLM 调用效率高** | 一次调用生成完整代码 |
| **整体速度快** | 无需多轮交互 |

#### 竞品方案的劣势

| 劣势 | 说明 |
|------|------|
| **错误影响范围大** | 代码块中间出错，整块需重来 |
| **状态回滚困难** | 难以精确知道执行到哪一步出错 |
| **注释可能误提取** | 普通注释也可能被当作步骤 |

### 13.5 优化方案：流式注释提取 + 分段预执行

结合两种方案优势，提出**流式注释提取 + 分段预执行**方案：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                  流式注释提取 + 分段预执行 方案                               │
└─────────────────────────────────────────────────────────────────────────────┘

核心思路：
  1. LLM 一次生成完整代码块（包含多个 @step）
  2. 流式解析，检测到 # @step: 时立即发送 notification
  3. 检测到下一个 @step 时，将上一段代码异步提交执行
  4. 执行失败则中断，进入修复流程

执行流程：
┌──────────────────────────────────────────────────────────────────────────────┐
│  时间线 ───────────────────────────────────────────────────────────────────→ │
│                                                                              │
│  LLM 流式输出:                                                               │
│                                                                              │
│  "<|begin_code|>\n"                                                          │
│  "# @step: 加载Excel数据\n"  ───→ 立即发送 notification                      │
│  "import pandas as pd\n"          {"step": "加载Excel数据"}                  │
│  "df = pd.read_excel(...)\n"                                                 │
│  "print(df.shape)\n"                                                         │
│  "\n"                                                                        │
│  "# @step: 查看数据结构\n"   ───→ ① 发送 notification                        │
│         │                         {"step": "查看数据结构"}                   │
│         │                                                                    │
│         └───────────────────→ ② 检测到新 @step，上一段代码完整               │
│                                  异步提交执行 "加载Excel数据" 代码段          │
│                                                                              │
│  "print(df.dtypes)\n"                                                        │
│  "print(df.head())\n"                执行结果返回                            │
│  "\n"                                     │                                  │
│  "# @step: 数据分析\n"       ───→ ├─ 成功：继续                              │
│         │                         └─ 失败：中断 LLM，修复                    │
│         │                                                                    │
│         └───────────────────→ 异步执行 "查看数据结构" 代码段                  │
│                                                                              │
│  ...(继续)                                                                   │
│                                                                              │
│  "<|end_code|>"              ───→ 代码块结束，执行最后一段                    │
└──────────────────────────────────────────────────────────────────────────────┘

用户视角（与竞品类似）：
┌─────────────────────────────────────────────────────────────────────────────┐
│  ① 步骤列表快速出现（流式提取）                                               │
│     - 加载Excel数据                                                          │
│     - 查看数据结构                                                           │
│     - 数据分析                                                               │
│                                                                              │
│  ② 代码逐渐展示                                                              │
│                                                                              │
│  ③ 执行结果陆续返回（分段预执行）                                             │
│                                                                              │
│  整体感知：步骤先出现，代码边生成边执行，响应快                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 13.6 流式注释提取实现要点

#### 1. 流式解析器

```
StreamingStepExtractor:
  
  状态：
    - current_buffer: 当前行缓冲
    - current_step: 当前步骤名
    - current_code: 当前步骤代码
    - steps_extracted: 已提取的步骤列表
  
  处理逻辑：
    on_token(token):
      current_buffer += token
      
      if current_buffer.endswith('\n'):
        line = current_buffer.strip()
        current_buffer = ''
        
        if line.startswith('# @step:'):
          # 检测到新步骤
          step_name = line.replace('# @step:', '').strip()
          
          # 1. 立即发送 notification
          send_notification(step_name)
          
          # 2. 如果有上一段代码，提交执行
          if current_code:
            async_execute(current_step, current_code)
          
          # 3. 开始新段落
          current_step = step_name
          current_code = ''
        else:
          # 普通代码行，追加到当前段落
          current_code += line + '\n'
```

#### 2. Notification 发送时机优化

```
发送时机对比：

原方案：
  LLM 生成完整代码 → 解析 @step → 发送 notification → 执行
  [─────5秒─────]   [─0.1秒─]    [───────]          [─2秒─]
  
  用户等待 5 秒才看到第一个步骤

优化后：
  LLM 流式输出 ────────────────────────────────→
       │
       ├─ 0.5秒: 检测到第一个 @step → 立即发送 notification
       ├─ 1.0秒: 检测到第二个 @step → 立即发送 notification
       │                              同时执行第一段代码
       ├─ 1.5秒: 检测到第三个 @step → 立即发送 notification
       │                              同时执行第二段代码
       ...
  
  用户 0.5 秒就能看到第一个步骤
```

#### 3. 分段预执行协调

```
AsyncExecutionCoordinator:
  
  执行队列管理：
    - pending_segments: 待执行的代码段队列
    - executing_segment: 正在执行的代码段
    - completed_segments: 已完成的代码段
  
  错误处理策略：
    on_execution_error(segment, error):
      # 1. 停止 LLM 生成（如果还在生成）
      cancel_llm_stream()
      
      # 2. 记录错误位置
      failed_step = segment.step_name
      
      # 3. 进入修复流程
      # 将错误信息追加到对话历史，只修复失败的那一段
      enter_repair_mode(failed_step, error)
```

### 13.7 方案对比总结

| 维度 | 我们原方案 | 竞品方案 | 优化方案 |
|------|-----------|---------|---------|
| 注释提取 | 代码完成后 | 流式实时 | 流式实时 |
| 步骤展示 | notification | 列表前置 | notification（实时） |
| 执行模式 | 逐步串行 | 整块执行 | 分段预执行 |
| 错误恢复 | 精确单步 | 整块重试 | 精确单步 |
| LLM 调用 | 多次 | 一次 | 一次 |
| 用户感知 | 慢 | 快 | 快 |
| 实现复杂度 | 低 | 低 | 中 |

### 13.8 实现优先级建议

**Phase 1：基础功能**
- 实现标准的 @step 注释解析
- 代码生成完毕后批量发送 notification
- 整块代码执行

**Phase 2：流式通知（关键优化）**
- LLM 流式输出
- 实时检测 @step 注释
- 检测到立即发送 notification
- **此阶段可达到竞品级别的用户感知速度**

**Phase 3：分段预执行（可选进阶）**
- @step 边界检测触发执行
- 异步执行协调
- 执行失败中断机制
- 精确错误恢复

### 13.9 结论

1. **竞品速度快的本质原因**：流式注释提取，步骤信息先于代码展示，用户感知延迟低

2. **竞品与我们方案的相同点**：都通过代码注释标记步骤，都需要解析提取

3. **关键优化点**：
   - **Phase 2（流式通知）是性价比最高的优化**
   - 只需改造注释提取时机，无需改变执行模式
   - 可达到接近竞品的用户体验

4. **我们方案的独特优势**：
   - `@step` 前缀避免普通注释误提取
   - 分段预执行可实现精确错误恢复
   - 更适合复杂的多步骤分析任务


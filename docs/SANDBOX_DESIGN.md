# TableMind Python 代码执行沙盒设计方案

## 一、项目背景与需求分析

### 1.1 项目背景

TableMind 是一个智能表格数据分析助手，通过 MCP（Model Context Protocol）工具为用户提供数据分析能力。当前代码执行直接在本地进程中使用 `exec()` 执行，存在以下问题：

- **安全风险**：LLM 生成的代码可能包含恶意操作
- **资源隔离**：无法限制资源使用，可能影响主服务
- **并发能力**：单进程执行，无法支持高并发请求
- **环境污染**：执行环境可能相互影响

### 1.2 核心需求

| 序号 | 需求 | 优先级 | 说明 |
|------|------|--------|------|
| 1 | MCP 工具内调用 | P0 | 代码执行集成在 MCP 工具内部，不作为独立工具暴露 |
| 2 | 文件读写支持 | P0 | 支持代码中的 `pd.read_csv('/path')` 和 `df.to_csv('/path')` |
| 3 | 返回 DataFrame 信息 | P0 | 执行结果需返回 df 预览、shape、columns 等信息 |
| 4 | 高并发支持 | P0 | 支持多用户同时分析，容器池预热 |
| 5 | 流式输出 | P1 | 实时返回 print 输出，提升用户体验 |
| 6 | 离线部署 | P0 | 服务器无外网，所有依赖需打包到镜像 |
| 7 | 状态信息返回 | P1 | 返回变量信息、执行耗时等元数据 |

### 1.3 部署约束

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           生产环境约束                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  • 服务器无外网访问                                                      │
│  • 需支持 Docker 部署                                                    │
│  • 数据文件存储在宿主机指定目录                                           │
│  • Python 数据分析库需预装（pandas, numpy, scipy, sklearn 等）           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 二、方案设计

### 2.1 整体架构

基于对 codebox-api、ipybox、dify-sandbox、microsandbox 的分析，推荐采用 **基于 IPython + Docker 容器池** 的方案：

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              TableMind MCP Server                             │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │                     Analyze_data / Table_operation 工具                 │  │
│  │                                                                        │  │
│  │   1. LLM 生成代码                                                      │  │
│  │   2. 调用 SandboxClient 执行代码                                        │  │
│  │   3. 处理返回结果（DataFrame 信息、图表等）                              │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       │ HTTP API
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           Sandbox Gateway (负载均衡)                          │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  • 请求路由与负载均衡                                                    │  │
│  │  • 容器池管理（预热、回收、健康检查）                                     │  │
│  │  • 请求队列与并发控制                                                    │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
                    ▼                  ▼                  ▼
          ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
          │ Sandbox Worker 1│ │ Sandbox Worker 2│ │ Sandbox Worker N│
          │  ┌───────────┐  │ │  ┌───────────┐  │ │  ┌───────────┐  │
          │  │  IPython  │  │ │  │  IPython  │  │ │  │  IPython  │  │
          │  │   Shell   │  │ │  │   Shell   │  │ │  │   Shell   │  │
          │  └───────────┘  │ │  └───────────┘  │ │  └───────────┘  │
          │  ┌───────────┐  │ │  ┌───────────┐  │ │  ┌───────────┐  │
          │  │ FastAPI   │  │ │  │ FastAPI   │  │ │  │ FastAPI   │  │
          │  │  Server   │  │ │  │  Server   │  │ │  │  Server   │  │
          │  └───────────┘  │ │  └───────────┘  │ │  └───────────┘  │
          │                 │ │                 │ │                 │
          │  /data (挂载)   │ │  /data (挂载)   │ │  /data (挂载)   │
          └─────────────────┘ └─────────────────┘ └─────────────────┘
                    │                  │                  │
                    └──────────────────┼──────────────────┘
                                       │
                                       ▼
                    ┌─────────────────────────────────────────┐
                    │         宿主机数据目录 /srv/data         │
                    │  ├── user_001/                          │
                    │  │   ├── uploads/                       │
                    │  │   └── outputs/                       │
                    │  └── user_002/                          │
                    └─────────────────────────────────────────┘
```

### 2.2 核心组件设计

#### 2.2.1 Sandbox Worker（代码执行容器）

**设计理念**：借鉴 codebox-api 的 LocalBox 实现，使用 IPython InteractiveShell 作为执行引擎。

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            Sandbox Worker 容器                                │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                          FastAPI Server (:8069)                         ││
│  │                                                                         ││
│  │  ┌─────────────────────────────────────────────────────────────────┐   ││
│  │  │ POST /exec                                                      │   ││
│  │  │ 统一流式接口：执行代码 + 流式输出 + 返回 DataFrame 信息           │   ││
│  │  └─────────────────────────────────────────────────────────────────┘   ││
│  │                                                                         ││
│  │  ┌─────────────────┐              ┌─────────────────┐                  ││
│  │  │ GET /health     │              │ POST /reset     │                  ││
│  │  │ 健康检查        │              │ 重置内核        │                  ││
│  │  └─────────────────┘              └─────────────────┘                  ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                                      │                                       │
│                                      ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                        IPython InteractiveShell                         ││
│  │  • 状态保持：变量、导入在会话间保持                                       ││
│  │  • 输出捕获：重定向 stdout/stderr 到 StringIO                            ││
│  │  • 图像处理：Patch matplotlib.pyplot.show() 输出 base64                  ││
│  │  • 超时控制：支持执行超时                                                 ││
│  │  • 结果提取：执行结束后自动提取指定变量的 DataFrame 信息                   ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                                      │                                       │
│  ┌──────────────────────┐    ┌───────────────────────────────────────────┐  │
│  │   预装 Python 库      │    │              挂载目录                      │  │
│  │  • pandas            │    │  /data → 宿主机数据目录（可读写）          │  │
│  │  • numpy             │    │  /app  → 工作目录                          │  │
│  │  • scipy             │    └───────────────────────────────────────────┘  │
│  │  • scikit-learn      │                                                    │
│  │  • matplotlib        │                                                    │
│  │  • seaborn           │                                                    │
│  │  • statsmodels       │                                                    │
│  │  • openpyxl          │                                                    │
│  └──────────────────────┘                                                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

#### 2.2.2 Sandbox Gateway（网关服务）

**职责**：容器池管理、请求路由、并发控制

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Sandbox Gateway                                  │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                            容器池管理器                                   ││
│  │  ┌─────────────────────────────────────────────────────────────────────┐││
│  │  │                        ContainerPool                                │││
│  │  │  • idle_containers: Queue[Container]    # 空闲容器队列              │││
│  │  │  • busy_containers: Dict[str, Container] # 使用中容器              │││
│  │  │  • min_pool_size: int = 3               # 最小池大小               │││
│  │  │  • max_pool_size: int = 10              # 最大池大小               │││
│  │  │  • container_ttl: int = 30 * 60         # 容器生存时间(秒)          │││
│  │  │                                                                    │││
│  │  │  async def acquire() -> Container       # 获取容器                 │││
│  │  │  async def release(container)           # 释放容器                 │││
│  │  │  async def scale_up()                   # 扩容                     │││
│  │  │  async def scale_down()                 # 缩容                     │││
│  │  │  async def health_check()               # 健康检查                 │││
│  │  └─────────────────────────────────────────────────────────────────────┘││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                           请求路由与负载均衡                              ││
│  │  • 轮询/最少连接数策略                                                   ││
│  │  • 请求超时处理                                                          ││
│  │  • 失败重试机制                                                          ││
│  └─────────────────────────────────────────────────────────────────────────┘│
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────┐│
│  │                              并发控制                                     ││
│  │  • max_concurrent_requests: int = 50       # 最大并发请求数              ││
│  │  • request_queue_size: int = 100           # 请求队列大小                ││
│  │  • request_timeout: int = 60               # 单请求超时(秒)               ││
│  └─────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 高并发架构对比

| 方案 | 优点 | 缺点 | 推荐场景 |
|------|------|------|----------|
| **方案A: 容器池预热** | 启动快、资源可控 | 需要管理容器生命周期 | ✅ **推荐**：中等并发 |
| 方案B: 容器内多进程 | 单容器、管理简单 | 隔离性差、资源竞争 | 低并发 |
| 方案C: K8s 动态扩缩 | 弹性强、云原生 | 复杂、冷启动慢 | 云环境 |

**推荐方案A：容器池预热**

```
请求流程:
                                                                   
    请求到达 ──► Gateway ──► 从池中获取空闲容器 ──► 执行代码 ──► 返回容器到池
                   │
                   ▼
              池满则等待或拒绝
```

---

## 三、核心接口设计

### 3.1 统一流式接口设计理念

**核心原则**：只有一个执行接口 `/exec`，全程流式输出，在流的最后输出执行结果（包含 DataFrame 信息）。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            统一流式协议                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  执行过程中（流式输出）：                                                     │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  <txt>print 输出内容...</txt>           ← 标准输出（实时）            │   │
│  │  <txt>更多输出...</txt>                                              │   │
│  │  <err>警告或错误信息...</err>           ← 错误输出（实时）            │   │
│  │  <img>base64 图像数据...</img>          ← 图像输出（如 plt.show）    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  执行结束（流的最后一个 chunk）：                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  <result>{                              ← 完整执行结果（JSON）        │   │
│  │      "success": true,                                                │   │
│  │      "execution_time": 0.523,                                        │   │
│  │      "dataframe": { ... },              ← DataFrame 详细信息         │   │
│  │      "variables": { ... }               ← 变量信息                   │   │
│  │  }</result>                                                          │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Sandbox Worker API

#### 3.2.1 执行代码（统一流式接口）

```yaml
POST /exec
Content-Type: application/json

Request:
{
    "code": "...",
    "timeout": 30,                    # 可选，执行超时时间(秒)，默认 30
    "cwd": "/data",                   # 可选，工作目录
    "result_var": "result",           # 可选，要提取 DataFrame 信息的变量名（不指定则不提取）
    "preview_rows": 10                # 可选，DataFrame 预览行数，默认 10
}
```

**场景一：数据分析（需要返回 DataFrame）**

代码通过 `return` 返回结果，需要指定 `result_var` 来提取 DataFrame 信息：

```yaml
Request:
{
    "code": "import pandas as pd\ndef analyze(df):\n    print('开始分析...')\n    result = df.describe()\n    print('分析完成')\n    return result\nresult = analyze(pd.read_csv('/data/test.csv'))",
    "result_var": "result",           # 指定要提取信息的变量
    "preview_rows": 10
}

Response: (Streaming)
<txt>开始分析...
</txt>
<txt>分析完成
</txt>
<result>{
    "success": true,
    "execution_time": 0.523,
    "error": null,
    "dataframe": {                    # ← 提取了 result 变量的 DataFrame 信息
        "var_name": "result",
        "shape": [8, 5],
        "columns": ["count", "mean", "std", "min", "max"],
        "dtypes": {...},
        "preview": [...]
    }
}</result>
```

**场景二：表格操作（不需要返回 DataFrame，直接写文件）**

代码直接写入文件，不需要返回 DataFrame 信息，不指定 `result_var`：

```yaml
Request:
{
    "code": "import pandas as pd\ndef operation(df, output_path):\n    print('开始处理...')\n    df['new_col'] = df['col1'] * 2\n    df.to_csv(output_path, index=False)\n    print(f'已保存到 {output_path}')\noperation(pd.read_csv('/data/input.csv'), '/data/output.csv')",
    "result_var": null                # 不指定，不提取 DataFrame 信息
}

Response: (Streaming)
<txt>开始处理...
</txt>
<txt>已保存到 /data/output.csv
</txt>
<result>{
    "success": true,
    "execution_time": 0.312,
    "error": null,
    "dataframe": null                 # ← 没有 DataFrame 信息
}</result>
```

#### 3.2.2 执行失败时的响应

```yaml
Response: (Streaming - 执行失败)

# 执行过程的输出仍然会流式返回
<txt>开始处理数据...
</txt>

# 错误信息
<err>Traceback (most recent call last):
  File "<string>", line 3, in <module>
FileNotFoundError: [Errno 2] No such file or directory: '/data/missing.csv'
</err>

# 最后的结果仍然返回，但 success=false
<result>{
    "success": false,
    "execution_time": 0.012,
    "error": "FileNotFoundError: [Errno 2] No such file or directory: '/data/missing.csv'",
    "dataframe": null,
    "variables": {}
}</result>
```

#### 3.2.3 健康检查

```yaml
GET /health

Response:
{
    "status": "healthy",
    "worker_id": "sandbox-worker-1",
    "uptime": "1h 30m",
    "kernel_status": "idle",
    "memory_usage": "256MB"
}
```

#### 3.2.4 重置内核

```yaml
POST /reset

Response:
{
    "success": true,
    "message": "Kernel reset successfully"
}
```

### 3.3 Sandbox Gateway API

Gateway 透传 Worker 的流式响应，增加请求路由和池管理功能。

#### 3.3.1 执行代码（透传流式）

```yaml
POST /api/v1/exec
Content-Type: application/json
X-Request-ID: uuid-xxx        # 可选，请求追踪

Request:
{
    "code": "...",
    "timeout": 30,
    "result_var": "result",
    "include_df_info": true,
    "preview_rows": 10
}

Response: (Streaming - 透传 Worker 的流式响应)
# 与 Worker /exec 接口响应格式完全相同
<txt>...</txt>
<img>...</img>
<result>...</result>
```

#### 3.3.2 获取池状态

```yaml
GET /api/v1/pool/status

Response:
{
    "total_containers": 5,
    "idle_containers": 3,
    "busy_containers": 2,
    "pending_requests": 0
}
```

### 3.4 流式协议标签说明

| 标签 | 含义 | 出现时机 | 示例 |
|------|------|----------|------|
| `<txt>...</txt>` | 标准输出 | 代码执行过程中（print 输出） | `<txt>Hello World</txt>` |
| `<err>...</err>` | 错误/警告输出 | 代码执行过程中（stderr） | `<err>Warning: ...</err>` |
| `<img>...</img>` | Base64 图像 | 调用 plt.show() 时 | `<img>iVBORw0...</img>` |
| `<result>...</result>` | 执行结果 JSON | **流的最后一个 chunk** | `<result>{"success":true,...}</result>` |

### 3.5 客户端解析流程

```python
async def parse_stream(response):
    """解析流式响应"""
    stdout_chunks = []
    stderr_chunks = []
    images = []
    result = None
    
    async for chunk in response.aiter_text():
        # 解析 <txt>...</txt>
        for match in re.finditer(r'<txt>(.*?)</txt>', chunk, re.DOTALL):
            content = match.group(1)
            stdout_chunks.append(content)
            yield {"type": "stdout", "content": content}  # 实时回调
        
        # 解析 <err>...</err>
        for match in re.finditer(r'<err>(.*?)</err>', chunk, re.DOTALL):
            content = match.group(1)
            stderr_chunks.append(content)
            yield {"type": "stderr", "content": content}
        
        # 解析 <img>...</img>
        for match in re.finditer(r'<img>(.*?)</img>', chunk, re.DOTALL):
            images.append(match.group(1))
            yield {"type": "image", "content": match.group(1)}
        
        # 解析 <result>...</result>（最后一个）
        match = re.search(r'<result>(.*?)</result>', chunk, re.DOTALL)
        if match:
            result = json.loads(match.group(1))
            yield {"type": "result", "content": result}
    
    return result
```

---

## 四、数据流设计

### 4.1 代码执行流程

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     代码执行完整流程（统一流式接口）                           │
└─────────────────────────────────────────────────────────────────────────────┘

 ①  MCP Tool 调用
     Analyze_data({
       "file_path": "/data/sales.csv",
       "analysis_type": "descriptive"
     })
            │
            ▼
 ②  代码生成 (由 LLM 生成)
     ┌────────────────────────────────────────────────────────────────┐
     │  import pandas as pd                                           │
     │  import numpy as np                                            │
     │                                                                │
     │  df = pd.read_csv("/data/sales.csv")                           │
     │  print(f"✓ 数据加载完成，形状: {df.shape}")                     │
     │  result = df.describe()                                        │
     │  print("✓ 分析完成")                                           │
     └────────────────────────────────────────────────────────────────┘
            │
            ▼
 ③  调用 SandboxClient（统一流式接口）
     sandbox_client.execute(
         code=generated_code,
         result_var="result",      # 要提取信息的变量
         include_df_info=True,     # 返回 DataFrame 详细信息
         on_stdout=handle_output   # 实时输出回调
     )
            │
            ▼
 ④  Gateway 处理（透传流式响应）
     ┌────────────────────────────────────────────────────────────────┐
     │  1. 从容器池获取空闲 Worker                                     │
     │  2. 建立流式连接，透传 Worker 的流式响应                         │
     │  3. 执行完成后释放 Worker 回池                                  │
     └────────────────────────────────────────────────────────────────┘
            │
            ▼
 ⑤  Worker 流式执行
     ┌────────────────────────────────────────────────────────────────┐
     │  IPython Shell 执行，实时输出：                                  │
     │                                                                │
     │  → <txt>✓ 数据加载完成，形状: (1000, 5)</txt>   ← 实时推送     │
     │  → <txt>✓ 分析完成</txt>                       ← 实时推送     │
     │                                                                │
     │  执行结束后，提取 result 变量的 DataFrame 信息：                 │
     │                                                                │
     │  → <result>{                                   ← 最后推送     │
     │        "success": true,                                        │
     │        "execution_time": 0.523,                                │
     │        "dataframe": {                                          │
     │            "var_name": "result",                               │
     │            "shape": [8, 5],                                    │
     │            "columns": [...],                                   │
     │            "preview": [...]                                    │
     │        },                                                      │
     │        "variables": {...}                                      │
     │    }</result>                                                  │
     └────────────────────────────────────────────────────────────────┘
            │
            ▼
 ⑥  客户端实时处理
     ┌────────────────────────────────────────────────────────────────┐
     │  • 实时回调 on_stdout：显示执行进度                              │
     │  • 收到 <result> 后解析最终结果                                 │
     │  • 提取 DataFrame 信息                                         │
     └────────────────────────────────────────────────────────────────┘
            │
            ▼
 ⑦  MCP Tool 处理结果
     ┌────────────────────────────────────────────────────────────────┐
     │  • 格式化 DataFrame 为 Markdown 表格                            │
     │  • 添加分析解读                                                 │
     │  • 返回给用户                                                   │
     └────────────────────────────────────────────────────────────────┘
```

### 4.2 流式输出时序图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        统一流式接口时序                                       │
└─────────────────────────────────────────────────────────────────────────────┘

  Client              Gateway              Worker (IPython)
    │                    │                       │
    │  POST /exec        │                       │
    │───────────────────►│  转发请求              │
    │                    │──────────────────────►│
    │                    │                       │
    │                    │                       │ print("Step 1...")
    │                    │  <txt>Step 1...</txt> │
    │  <txt>Step 1...</txt>◄─────────────────────│
    │◄───────────────────│                       │
    │  ↑ 实时回调        │                       │
    │                    │                       │ print("Step 2...")
    │                    │  <txt>Step 2...</txt> │
    │  <txt>Step 2...</txt>◄─────────────────────│
    │◄───────────────────│                       │
    │  ↑ 实时回调        │                       │
    │                    │                       │ plt.show()
    │                    │  <img>base64...</img> │
    │  <img>base64...</img>◄────────────────────│
    │◄───────────────────│                       │
    │                    │                       │
    │                    │                       │ 执行完成
    │                    │                       │ 提取 df 信息
    │                    │  <result>{...}</result>
    │  <result>{...}</result>◄──────────────────│
    │◄───────────────────│                       │
    │                    │                       │
    │  完成解析          │  释放 Worker          │
    │                    │                       │
```

### 4.3 关键设计：流式输出 + 可选的 DataFrame 提取

**核心逻辑：根据 `result_var` 参数决定是否提取 DataFrame 信息**

```
执行过程：
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. 代码在 IPython 中执行                                                    │
│     ↓                                                                       │
│  2. stdout/stderr 被重定向到缓冲区                                           │
│     ↓                                                                       │
│  3. 主线程轮询缓冲区，有内容就 yield <txt>/<err>（流式输出）                   │
│     ↓                                                                       │
│  4. 执行完成后，检查 result_var 参数：                                        │
│     ├── 如果指定了 result_var：                                              │
│     │   └── 提取该变量的 DataFrame 信息（shape/columns/preview）             │
│     └── 如果 result_var 为 None：                                            │
│         └── 不提取 DataFrame 信息                                            │
│     ↓                                                                       │
│  5. yield <result>{...}</result>（流的最后一个 chunk）                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

**两种使用场景：**

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  场景一：数据分析（Analyze_data）                                             │
│  ───────────────────────────────                                            │
│  代码：def analyze(df): return df.describe()                                 │
│  调用：execute(code, result_var="result")                                   │
│  返回：                                                                      │
│    <txt>分析中...</txt>                                                     │
│    <result>{                                                                │
│      "success": true,                                                       │
│      "dataframe": {         ← 包含 DataFrame 信息                           │
│        "shape": [8, 5],                                                     │
│        "preview": [...]                                                     │
│      }                                                                      │
│    }</result>                                                               │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  场景二：表格操作（Table_operation）                                          │
│  ───────────────────────────────────                                        │
│  代码：def operation(...): df.to_csv(output_path)                           │
│  调用：execute(code, result_var=None)                                       │
│  返回：                                                                      │
│    <txt>处理中...</txt>                                                     │
│    <txt>已保存到 /data/output.csv</txt>                                     │
│    <result>{                                                                │
│      "success": true,                                                       │
│      "dataframe": null      ← 不包含 DataFrame 信息（结果已写入文件）         │
│    }</result>                                                               │
└─────────────────────────────────────────────────────────────────────────────┘
```

**设计优势：**
- 一个接口满足所有场景，通过参数控制行为
- 流式输出与结果返回统一，无需额外调用
- 表格操作场景避免了不必要的 DataFrame 序列化开销

---

## 五、文件系统设计

### 5.1 目录结构

```
宿主机:
/srv/tablemind/
├── data/                              # 数据目录（挂载到容器）
│   ├── shared/                        # 共享数据
│   │   ├── datasets/                  # 公共数据集
│   │   └── templates/                 # 模板文件
│   └── sessions/                      # 用户会话数据
│       ├── session-abc123/
│       │   ├── uploads/               # 用户上传文件
│       │   ├── outputs/               # 分析输出
│       │   └── temp/                  # 临时文件
│       └── session-def456/
├── logs/                              # 日志目录
└── config/                            # 配置文件

容器内:
/
├── app/                               # 应用目录
│   ├── sandbox_worker/                # Worker 代码
│   └── codebox/                       # 工作目录
├── data/                              # 挂载：宿主机 /srv/tablemind/data
│   ├── shared/                        # 只读挂载
│   └── sessions/                      # 读写挂载
└── tmp/                               # 临时目录
```

### 5.2 挂载策略

```yaml
# Docker Compose 示例
services:
  sandbox-worker:
    image: tablemind/sandbox:latest
    volumes:
      # 共享数据（只读）
      - /srv/tablemind/data/shared:/data/shared:ro
      # 会话数据（读写）
      - /srv/tablemind/data/sessions:/data/sessions:rw
    environment:
      - SANDBOX_DATA_ROOT=/data
```

### 5.3 路径映射

代码中的路径会被自动映射：

| 代码中的路径 | 容器内实际路径 | 宿主机路径 |
|-------------|---------------|-----------|
| `/data/shared/dataset.csv` | `/data/shared/dataset.csv` | `/srv/tablemind/data/shared/dataset.csv` |
| `/data/output.csv` | `/data/sessions/{session_id}/output.csv` | `/srv/tablemind/data/sessions/{session_id}/output.csv` |

---

## 六、安全设计

### 6.1 安全层级

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              安全防护层次                                    │
│                                                                             │
│  第1层: Docker 容器隔离                                                     │
│  ┌───────────────────────────────────────────────────────────────────────┐ │
│  │  • 独立的文件系统命名空间                                               │ │
│  │  • 独立的进程命名空间                                                   │ │
│  │  • 资源限制 (CPU/Memory)                                               │ │
│  │                                                                       │ │
│  │  第2层: 网络隔离                                                       │ │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │ │
│  │  │  • 禁用外网访问 (--network none 或自定义网络)                     │  │ │
│  │  │  • 仅允许与 Gateway 通信                                         │  │ │
│  │  │                                                                 │  │ │
│  │  │  第3层: 文件系统限制                                             │  │ │
│  │  │  ┌───────────────────────────────────────────────────────────┐  │  │ │
│  │  │  │  • 只读挂载系统目录                                         │  │  │ │
│  │  │  │  • 限制可写目录范围                                         │  │  │ │
│  │  │  │  • 磁盘配额限制                                             │  │  │ │
│  │  │  │                                                           │  │  │ │
│  │  │  │  第4层: 用户权限                                           │  │  │ │
│  │  │  │  ┌─────────────────────────────────────────────────────┐  │  │  │ │
│  │  │  │  │  • 非 root 用户运行                                   │  │  │  │ │
│  │  │  │  │  • 最小权限原则                                       │  │  │  │ │
│  │  │  │  │                                                     │  │  │  │ │
│  │  │  │  │  第5层: 代码审查 (可选)                               │  │  │  │ │
│  │  │  │  │  ┌───────────────────────────────────────────────┐  │  │  │  │ │
│  │  │  │  │  │  • 危险模块检测                                 │  │  │  │  │ │
│  │  │  │  │  │  • 禁用 os.system, subprocess 等               │  │  │  │  │ │
│  │  │  │  │  └───────────────────────────────────────────────┘  │  │  │  │ │
│  │  │  │  └─────────────────────────────────────────────────────┘  │  │  │ │
│  │  │  └───────────────────────────────────────────────────────────┘  │  │ │
│  │  └─────────────────────────────────────────────────────────────────┘  │ │
│  └───────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Docker 安全配置

```yaml
# docker-compose.yml 安全配置示例
services:
  sandbox-worker:
    image: tablemind/sandbox:latest
    
    # 资源限制
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G
        reservations:
          cpus: '0.5'
          memory: 512M
    
    # 安全配置
    security_opt:
      - no-new-privileges:true      # 禁止提权
    cap_drop:
      - ALL                         # 删除所有能力
    cap_add:
      - CHOWN                       # 只添加必要能力
      - SETUID
      - SETGID
    
    # 只读根文件系统
    read_only: true
    tmpfs:
      - /tmp:size=100M
      - /app/codebox:size=500M
    
    # 网络隔离
    networks:
      - sandbox-internal
    
    # 非 root 用户
    user: "1000:1000"

networks:
  sandbox-internal:
    internal: true                  # 禁止外部访问
```

### 6.3 代码审查（可选增强）

```python
# 危险模式检测
DANGEROUS_PATTERNS = [
    r'\bos\.system\b',
    r'\bsubprocess\b',
    r'\beval\b',
    r'\bexec\b',           # 注意：我们自己在用 exec，需要区分
    r'\b__import__\b',
    r'\bopen\s*\([^)]*["\']w',  # 写文件
    r'\brequests\.get\b',
    r'\burllib\b',
]

def check_code_safety(code: str) -> tuple[bool, list[str]]:
    """检查代码安全性"""
    warnings = []
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, code):
            warnings.append(f"检测到潜在危险操作: {pattern}")
    return len(warnings) == 0, warnings
```

---

## 七、容器镜像设计

### 7.1 Dockerfile

```dockerfile
# tablemind-sandbox/Dockerfile

# ============================================
# 阶段1: 构建阶段
# ============================================
FROM python:3.11-slim-bookworm AS builder

# 安装构建依赖
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 安装 Python 依赖
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ============================================
# 阶段2: 运行时阶段
# ============================================
FROM python:3.11-slim-bookworm AS runtime

# 安装运行时依赖
RUN apt-get update && apt-get install -y \
    # 数值计算库
    libopenblas-dev \
    liblapack-dev \
    # 字体支持（matplotlib）
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 创建非 root 用户
RUN groupadd -r sandbox && useradd -r -g sandbox -u 1000 sandbox

# 复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 设置 matplotlib 后端
ENV MPLBACKEND=Agg

# 复制应用代码
WORKDIR /app
COPY --chown=sandbox:sandbox ./sandbox_worker /app/sandbox_worker

# 创建工作目录
RUN mkdir -p /app/codebox /data && \
    chown -R sandbox:sandbox /app /data

# 切换到非 root 用户
USER sandbox

# 暴露端口
EXPOSE 8069

# 启动命令
CMD ["python", "-m", "sandbox_worker.api"]
```

### 7.2 依赖列表 (requirements.txt)

```
# 核心框架
fastapi==0.109.0
uvicorn[standard]==0.27.0
pydantic==2.5.3
httpx==0.26.0
aiofiles==23.2.1

# IPython 执行引擎
ipython==8.20.0

# 数据分析核心库
pandas==2.1.4
numpy==1.26.3
scipy==1.12.0

# 机器学习
scikit-learn==1.4.0
statsmodels==0.14.1

# 可视化
matplotlib==3.8.2
seaborn==0.13.1

# Excel 支持
openpyxl==3.1.2
xlrd==2.0.1

# 其他常用库
tqdm==4.66.1
python-dateutil==2.8.2
pytz==2024.1
```

### 7.3 镜像构建与离线部署

```bash
# 1. 构建镜像
docker build -t tablemind/sandbox:latest .

# 2. 导出镜像（用于离线部署）
docker save tablemind/sandbox:latest | gzip > tablemind-sandbox.tar.gz

# 3. 在目标服务器加载镜像
docker load < tablemind-sandbox.tar.gz
```

---

## 八、部署架构

### 8.1 单机部署（Docker Compose）

```yaml
# docker-compose.yml
version: '3.8'

services:
  # ==========================================
  # Sandbox Gateway
  # ==========================================
  sandbox-gateway:
    build: ./gateway
    image: tablemind/sandbox-gateway:latest
    container_name: sandbox-gateway
    ports:
      - "8080:8080"
    environment:
      - POOL_MIN_SIZE=3
      - POOL_MAX_SIZE=10
      - WORKER_TIMEOUT=30
      - DOCKER_HOST=unix:///var/run/docker.sock
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./config:/app/config:ro
    networks:
      - sandbox-network
    depends_on:
      - sandbox-worker-1
      - sandbox-worker-2
      - sandbox-worker-3
    restart: unless-stopped

  # ==========================================
  # Sandbox Workers (预热池)
  # ==========================================
  sandbox-worker-1:
    image: tablemind/sandbox:latest
    container_name: sandbox-worker-1
    environment:
      - WORKER_ID=1
      - CODEBOX_TIMEOUT=30
    volumes:
      - /srv/tablemind/data/shared:/data/shared:ro
      - /srv/tablemind/data/sessions:/data/sessions:rw
    networks:
      - sandbox-network
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G
    restart: unless-stopped

  sandbox-worker-2:
    image: tablemind/sandbox:latest
    container_name: sandbox-worker-2
    environment:
      - WORKER_ID=2
      - CODEBOX_TIMEOUT=30
    volumes:
      - /srv/tablemind/data/shared:/data/shared:ro
      - /srv/tablemind/data/sessions:/data/sessions:rw
    networks:
      - sandbox-network
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G
    restart: unless-stopped

  sandbox-worker-3:
    image: tablemind/sandbox:latest
    container_name: sandbox-worker-3
    environment:
      - WORKER_ID=3
      - CODEBOX_TIMEOUT=30
    volumes:
      - /srv/tablemind/data/shared:/data/shared:ro
      - /srv/tablemind/data/sessions:/data/sessions:rw
    networks:
      - sandbox-network
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G
    restart: unless-stopped

networks:
  sandbox-network:
    driver: bridge
    internal: true  # 禁止外网访问
```

### 8.2 部署拓扑

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              生产部署拓扑                                    │
└─────────────────────────────────────────────────────────────────────────────┘

                         ┌─────────────────────────────┐
                         │     TableMind MCP Server     │
                         │      (主应用服务)            │
                         └─────────────┬───────────────┘
                                       │
                                       │ HTTP :8080
                                       ▼
                         ┌─────────────────────────────┐
                         │     Sandbox Gateway         │
                         │    sandbox-gateway:8080     │
                         │   (容器池管理、负载均衡)     │
                         └─────────────┬───────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
              ▼                        ▼                        ▼
    ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
    │ sandbox-worker-1│     │ sandbox-worker-2│     │ sandbox-worker-3│
    │    :8069        │     │    :8069        │     │    :8069        │
    └────────┬────────┘     └────────┬────────┘     └────────┬────────┘
             │                       │                       │
             └───────────────────────┼───────────────────────┘
                                     │
                                     │ Docker Volume
                                     ▼
                         ┌─────────────────────────────┐
                         │   /srv/tablemind/data/       │
                         │   (宿主机数据存储)           │
                         └─────────────────────────────┘
```

---

## 九、客户端 SDK 设计

### 9.1 SandboxClient（统一流式接口）

```python
# sandbox_client.py - TableMind 中使用的客户端

import re
import json
from dataclasses import dataclass, field
from typing import Optional, AsyncGenerator, Callable, Any
import httpx


@dataclass
class DataFrameInfo:
    """DataFrame 信息"""
    var_name: str
    shape: tuple[int, int]
    columns: list[str]
    dtypes: dict[str, str]
    preview: list[dict]
    memory_usage: str


@dataclass
class ExecutionResult:
    """执行结果"""
    success: bool
    execution_time: float
    error: Optional[str] = None
    dataframe: Optional[DataFrameInfo] = None
    variables: Optional[dict] = None
    # 执行过程中收集的输出
    stdout_chunks: list[str] = field(default_factory=list)
    stderr_chunks: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    
    @property
    def stdout(self) -> str:
        """合并所有标准输出"""
        return "".join(self.stdout_chunks)
    
    @property
    def stderr(self) -> str:
        """合并所有错误输出"""
        return "".join(self.stderr_chunks)


@dataclass
class StreamChunk:
    """流式输出块"""
    type: str  # "stdout", "stderr", "image", "result"
    content: Any


class SandboxClient:
    """
    沙盒执行客户端
    
    使用统一的流式接口，全程流式输出，最后返回完整结果（含 DataFrame 信息）
    """
    
    def __init__(
        self,
        gateway_url: str = "http://localhost:8080",
        timeout: float = 60.0,
    ):
        self.gateway_url = gateway_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)
    
    async def execute(
        self,
        code: str,
        result_var: Optional[str] = None,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
        preview_rows: int = 10,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
        on_image: Optional[Callable[[str], None]] = None,
    ) -> ExecutionResult:
        """
        执行代码（统一接口，全程流式）
        
        Args:
            code: 要执行的 Python 代码
            result_var: 要提取 DataFrame 信息的变量名
                        - 指定时：提取该变量的 DataFrame 信息（用于数据分析场景）
                        - None 时：不提取 DataFrame 信息（用于表格操作/写文件场景）
            timeout: 执行超时时间（秒）
            cwd: 工作目录
            preview_rows: DataFrame 预览行数（仅当 result_var 指定时有效）
            on_stdout: 标准输出回调（实时）
            on_stderr: 错误输出回调（实时）
            on_image: 图像输出回调（实时）
            
        Returns:
            ExecutionResult: 完整执行结果
                - 如果指定了 result_var，包含 DataFrame 信息
                - 否则 dataframe 为 None
        """
        result = ExecutionResult(success=False, execution_time=0)
        
        async for chunk in self.stream_execute(
            code=code,
            result_var=result_var,
            timeout=timeout,
            cwd=cwd,
            include_df_info=include_df_info,
            preview_rows=preview_rows,
        ):
            if chunk.type == "stdout":
                result.stdout_chunks.append(chunk.content)
                if on_stdout:
                    on_stdout(chunk.content)
            elif chunk.type == "stderr":
                result.stderr_chunks.append(chunk.content)
                if on_stderr:
                    on_stderr(chunk.content)
            elif chunk.type == "image":
                result.images.append(chunk.content)
                if on_image:
                    on_image(chunk.content)
            elif chunk.type == "result":
                # 最后的结果
                result.success = chunk.content.get("success", False)
                result.execution_time = chunk.content.get("execution_time", 0)
                result.error = chunk.content.get("error")
                result.variables = chunk.content.get("variables")
                
                # 解析 DataFrame 信息
                if chunk.content.get("dataframe"):
                    df_data = chunk.content["dataframe"]
                    result.dataframe = DataFrameInfo(
                        var_name=df_data.get("var_name", ""),
                        shape=tuple(df_data["shape"]),
                        columns=df_data["columns"],
                        dtypes=df_data["dtypes"],
                        preview=df_data.get("preview", []),
                        memory_usage=df_data.get("memory_usage", ""),
                    )
        
        return result
    
    async def stream_execute(
        self,
        code: str,
        result_var: Optional[str] = None,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
        preview_rows: int = 10,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        流式执行代码，逐块返回输出
        
        Args:
            result_var: 指定时提取 DataFrame 信息，None 时不提取
        
        Yields:
            StreamChunk: 输出块（stdout/stderr/image/result）
        """
        async with self._client.stream(
            "POST",
            f"{self.gateway_url}/api/v1/exec",
            json={
                "code": code,
                "result_var": result_var,
                "timeout": timeout or self.timeout,
                "cwd": cwd,
                "preview_rows": preview_rows,
            },
        ) as response:
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                
                # 解析完整的标签
                while True:
                    # 尝试解析 <txt>...</txt>
                    match = re.search(r'<txt>(.*?)</txt>', buffer, re.DOTALL)
                    if match:
                        yield StreamChunk(type="stdout", content=match.group(1))
                        buffer = buffer[:match.start()] + buffer[match.end():]
                        continue
                    
                    # 尝试解析 <err>...</err>
                    match = re.search(r'<err>(.*?)</err>', buffer, re.DOTALL)
                    if match:
                        yield StreamChunk(type="stderr", content=match.group(1))
                        buffer = buffer[:match.start()] + buffer[match.end():]
                        continue
                    
                    # 尝试解析 <img>...</img>
                    match = re.search(r'<img>(.*?)</img>', buffer, re.DOTALL)
                    if match:
                        yield StreamChunk(type="image", content=match.group(1))
                        buffer = buffer[:match.start()] + buffer[match.end():]
                        continue
                    
                    # 尝试解析 <result>...</result>
                    match = re.search(r'<result>(.*?)</result>', buffer, re.DOTALL)
                    if match:
                        result_data = json.loads(match.group(1))
                        yield StreamChunk(type="result", content=result_data)
                        buffer = buffer[:match.start()] + buffer[match.end():]
                        continue
                    
                    # 没有找到完整标签，等待更多数据
                    break
    
    async def reset(self) -> bool:
        """重置执行环境"""
        response = await self._client.post(
            f"{self.gateway_url}/api/v1/reset"
        )
        return response.status_code == 200
    
    async def health_check(self) -> bool:
        """健康检查"""
        try:
            response = await self._client.get(
                f"{self.gateway_url}/health"
            )
            return response.status_code == 200
        except Exception:
            return False
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._client.aclose()
```

### 9.2 在 MCP 工具中使用

根据 TableMind 的设计，有两种典型场景：

- **数据分析 (Analyze_data)**：代码 `return result`，需要提取 DataFrame 信息
- **表格操作 (Table_operation)**：代码直接写文件，不需要返回 DataFrame

```python
# 在 MCP 工具中使用 SandboxClient

from sandbox_client import SandboxClient, ExecutionResult

class AnalyzeDataTool:
    """
    数据分析工具
    代码格式：def analyze(df): ... return result
    需要提取返回的 DataFrame 信息
    """
    
    def __init__(self):
        self.sandbox = SandboxClient(gateway_url="http://sandbox-gateway:8080")
    
    async def analyze(self, file_path: str, analysis_type: str) -> dict:
        """执行数据分析，需要返回 DataFrame"""
        
        code = self._generate_analysis_code(file_path, analysis_type)
        
        # 数据分析场景：指定 result_var 来提取 DataFrame 信息
        result = await self.sandbox.execute(
            code=code,
            result_var="result",        # ← 指定要提取的变量名
            timeout=30,
            preview_rows=20,
            on_stdout=lambda x: print(f"[分析进度] {x}", end=""),
        )
        
        if not result.success:
            return {"error": result.error or result.stderr, "success": False}
        
        return self._format_result(result)
    
    def _generate_analysis_code(self, file_path: str, analysis_type: str) -> str:
        """生成数据分析代码（由 LLM 生成）"""
        # 数据分析代码格式：定义 analyze 函数并返回结果
        return f'''
import pandas as pd
import numpy as np

def analyze(df):
    print("✓ 开始分析...")
    result = df.describe(include='all')
    print("✓ 分析完成")
    return result

df = pd.read_csv("{file_path}")
print(f"✓ 数据加载完成，形状: {{df.shape}}")
result = analyze(df)
'''
    
    def _format_result(self, result: ExecutionResult) -> dict:
        output = {
            "success": True,
            "execution_time": f"{result.execution_time:.2f}s",
            "output": result.stdout,
        }
        
        # 数据分析场景：result.dataframe 包含 DataFrame 信息
        if result.dataframe:
            df = result.dataframe
            output["dataframe"] = {
                "var_name": df.var_name,
                "shape": f"{df.shape[0]} 行 × {df.shape[1]} 列",
                "columns": df.columns,
                "preview": df.preview,
            }
        
        return output


class TableOperationTool:
    """
    表格操作工具
    代码格式：def operation(dfs, input_paths, output_path): ... df.to_csv(output_path)
    直接写文件，不需要返回 DataFrame
    """
    
    def __init__(self):
        self.sandbox = SandboxClient(gateway_url="http://sandbox-gateway:8080")
    
    async def operate(
        self, 
        instruction: str, 
        input_paths: list[str], 
        output_path: str
    ) -> dict:
        """执行表格操作，直接写文件"""
        
        code = self._generate_operation_code(instruction, input_paths, output_path)
        
        # 表格操作场景：不指定 result_var，不提取 DataFrame 信息
        result = await self.sandbox.execute(
            code=code,
            result_var=None,            # ← 不指定，不提取 DataFrame 信息
            timeout=30,
            on_stdout=lambda x: print(f"[操作进度] {x}", end=""),
        )
        
        if not result.success:
            return {"error": result.error or result.stderr, "success": False}
        
        return {
            "success": True,
            "output": result.stdout,
            "output_path": output_path,
            "execution_time": f"{result.execution_time:.2f}s",
            # 注意：这里没有 dataframe 信息，因为结果已写入文件
        }
    
    def _generate_operation_code(
        self, 
        instruction: str, 
        input_paths: list[str], 
        output_path: str
    ) -> str:
        """生成表格操作代码（由 LLM 生成）"""
        # 表格操作代码格式：定义 operation 函数，直接写文件
        input_paths_str = str(input_paths)
        return f'''
import pandas as pd
import os

def operation(dataframes, input_paths, output_path):
    print("✓ 开始处理...")
    df = dataframes[0]
    
    # 执行操作（由 LLM 根据 instruction 生成）
    df['new_column'] = df['existing_column'] * 2
    
    # 保存结果到文件
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"✓ 已保存到 {{output_path}}")

# 加载输入数据
dataframes = [pd.read_csv(p) for p in {input_paths_str}]
operation(dataframes, {input_paths_str}, "{output_path}")
'''


# ============ 使用示例 ============

async def example_usage():
    # 示例1：数据分析（需要返回 DataFrame）
    analyze_tool = AnalyzeDataTool()
    result = await analyze_tool.analyze(
        file_path="/data/sales.csv",
        analysis_type="descriptive"
    )
    print(result["dataframe"]["preview"])  # 获取 DataFrame 预览
    
    # 示例2：表格操作（直接写文件，不需要返回 DataFrame）
    operation_tool = TableOperationTool()
    result = await operation_tool.operate(
        instruction="新增一列，计算 price * quantity",
        input_paths=["/data/orders.csv"],
        output_path="/data/orders_with_total.csv"
    )
    print(f"文件已保存到: {result['output_path']}")
```

### 9.3 场景对比

| 场景 | result_var | DataFrame 返回 | 典型代码 |
|------|------------|---------------|----------|
| **数据分析** | 指定（如 `"result"`） | ✅ 返回 shape/columns/preview | `return df.describe()` |
| **表格操作** | `None` | ❌ 不返回 | `df.to_csv(output_path)` |

### 9.4 辅助方法

```python
def _to_markdown_table(
    self,
    columns: list[str],
    rows: list[dict]
) -> str:
        """转换为 Markdown 表格"""
        if not rows:
            return ""
        
        # 表头
        header = "| " + " | ".join(columns) + " |"
        separator = "| " + " | ".join(["---"] * len(columns)) + " |"
        
        # 数据行
        data_rows = []
        for row in rows[:10]:  # 最多显示10行
            values = [str(row.get(col, "")) for col in columns]
            data_rows.append("| " + " | ".join(values) + " |")
        
        return "\n".join([header, separator] + data_rows)
```

---

## 十、配置管理

### 10.1 配置文件结构

```yaml
# config/sandbox.yaml

# ==========================================
# Sandbox Gateway 配置
# ==========================================
gateway:
  host: "0.0.0.0"
  port: 8080
  
  # 容器池配置
  pool:
    min_size: 3              # 最小容器数
    max_size: 10             # 最大容器数
    scale_up_threshold: 0.8  # 扩容阈值（使用率）
    scale_down_threshold: 0.2 # 缩容阈值
    container_ttl: 1800      # 容器生存时间（秒）
    health_check_interval: 30 # 健康检查间隔（秒）
  
  # 请求控制
  request:
    max_concurrent: 50       # 最大并发请求
    queue_size: 100          # 请求队列大小
    default_timeout: 60      # 默认超时（秒）
    max_timeout: 300         # 最大超时（秒）

# ==========================================
# Sandbox Worker 配置
# ==========================================
worker:
  host: "0.0.0.0"
  port: 8069
  
  # 执行配置
  execution:
    default_timeout: 30      # 默认执行超时
    max_code_length: 100000  # 最大代码长度
    max_output_size: 10485760 # 最大输出大小（10MB）
  
  # IPython 配置
  ipython:
    matplotlib_backend: "Agg"
    max_variables: 100       # 最大变量数
  
  # 文件系统
  filesystem:
    work_dir: "/app/codebox"
    data_root: "/data"
    temp_dir: "/tmp"
    max_file_size: 104857600 # 最大文件大小（100MB）

# ==========================================
# Docker 配置
# ==========================================
docker:
  image: "tablemind/sandbox:latest"
  
  # 资源限制
  resources:
    cpu_limit: "1.0"
    memory_limit: "2g"
    memory_reservation: "512m"
  
  # 网络配置
  network:
    name: "sandbox-network"
    internal: true           # 禁止外网
  
  # 卷挂载
  volumes:
    shared_data:
      host_path: "/srv/tablemind/data/shared"
      container_path: "/data/shared"
      read_only: true
    session_data:
      host_path: "/srv/tablemind/data/sessions"
      container_path: "/data/sessions"
      read_only: false

# ==========================================
# 安全配置
# ==========================================
security:
  # 代码审查
  code_check:
    enabled: true
    block_dangerous: false   # 是否阻止危险代码
    warn_only: true          # 仅警告
  
  # 危险模式
  dangerous_patterns:
    - 'os\.system'
    - 'subprocess\.'
    - '__import__'
    - 'eval\s*\('

# ==========================================
# 日志配置
# ==========================================
logging:
  level: "INFO"
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
  file: "/var/log/sandbox/sandbox.log"
  max_size: 104857600        # 100MB
  backup_count: 5
```

---

## 十一、监控与运维

### 11.1 健康检查端点

```yaml
# Gateway 健康检查
GET /health
Response:
{
    "status": "healthy",
    "version": "1.0.0",
    "pool": {
        "total": 5,
        "idle": 3,
        "busy": 2
    },
    "uptime": "2d 3h 15m"
}

# Worker 健康检查
GET /health
Response:
{
    "status": "healthy",
    "worker_id": "sandbox-worker-1",
    "kernel_status": "idle",
    "memory_usage": "256MB",
    "uptime": "1h 30m"
}
```

### 11.2 监控指标

```
# Prometheus 指标示例

# Gateway 指标
sandbox_gateway_requests_total{method="POST", endpoint="/execute"} 1234
sandbox_gateway_request_duration_seconds{quantile="0.99"} 2.5
sandbox_gateway_pool_size{state="idle"} 3
sandbox_gateway_pool_size{state="busy"} 2
sandbox_gateway_queue_length 0

# Worker 指标
sandbox_worker_executions_total 567
sandbox_worker_execution_duration_seconds{quantile="0.99"} 1.2
sandbox_worker_memory_usage_bytes 268435456
sandbox_worker_active_variables 15
```

### 11.3 日志格式

```json
{
    "timestamp": "2025-01-06T10:30:00.000Z",
    "level": "INFO",
    "service": "sandbox-gateway",
    "request_id": "uuid-xxx",
    "message": "Code execution completed",
    "details": {
        "worker_id": "sandbox-worker-1",
        "execution_time": 0.523,
        "code_length": 256,
        "output_size": 1024
    }
}
```

---

## 十二、与现有系统集成

### 12.1 集成到 TableMind

```python
# TableMind/src/code_executor.py 改造

from sandbox_client import SandboxClient

class CodeExecutor:
    """代码执行器（沙盒版本）"""
    
    def __init__(
        self,
        data_accessor: BaseDataAccessor,
        llm: Optional[BaseLLM] = None,
        sandbox_url: str = "http://sandbox-gateway:8080"
    ):
        self.llm = llm
        self.data_accessor = data_accessor
        self.sandbox = SandboxClient(gateway_url=sandbox_url)
        self.logger = utils.get_logger(self.__class__.__name__)
    
    async def execute_analysis(self, question: str, code: str) -> pd.DataFrame:
        """
        执行数据分析代码（需要返回 DataFrame）
        对应 Analyze_data 工具
        """
        max_retry = config.get_config()['max_retry_execution_count']
        error_history = []
        
        while len(error_history) <= max_retry:
            try:
                # 数据分析：指定 result_var 提取 DataFrame 信息
                result = await self.sandbox.execute(
                    code=code,
                    result_var="result",       # ← 数据分析需要提取结果
                    timeout=30,
                    preview_rows=100,
                    on_stdout=lambda x: self.logger.info(f"[分析] {x}"),
                )
                
                if not result.success:
                    raise ExecutionError(result.error or result.stderr)
                
                # 从预览数据重建 DataFrame
                if result.dataframe and result.dataframe.preview:
                    return pd.DataFrame(result.dataframe.preview)
                
                return pd.DataFrame()
                
            except Exception as e:
                self.logger.warning(f"Execution failed: {e}")
                if len(error_history) + 1 > max_retry:
                    break
                error_history.append({"code": code, "error": str(e)})
                code = await self._correct_code(question, error_history)
        
        return pd.DataFrame()
    
    async def execute_operation(self, instruction: str, code: str, output_path: str) -> bool:
        """
        执行表格操作代码（直接写文件，不返回 DataFrame）
        对应 Table_operation 工具
        """
        max_retry = config.get_config()['max_retry_execution_count']
        error_history = []
        
        while len(error_history) <= max_retry:
            try:
                # 表格操作：不指定 result_var，结果直接写入文件
                result = await self.sandbox.execute(
                    code=code,
                    result_var=None,           # ← 表格操作不需要提取结果
                    timeout=30,
                    on_stdout=lambda x: self.logger.info(f"[操作] {x}"),
                )
                
                if not result.success:
                    raise ExecutionError(result.error or result.stderr)
                
                self.logger.info(f"表格操作完成，输出文件: {output_path}")
                return True
                
            except Exception as e:
                self.logger.warning(f"Execution failed: {e}")
                if len(error_history) + 1 > max_retry:
                    break
                error_history.append({"code": code, "error": str(e)})
                code = await self._correct_code(instruction, error_history)
        
        return False
    
    async def _correct_code(
        self,
        question: str,
        error_history: list
    ) -> str:
        """使用 LLM 修正代码"""
        # ... 实现代码修正逻辑
        pass
```

### 12.2 数据文件路径处理

```python
# 路径映射工具

class PathMapper:
    """路径映射器"""
    
    def __init__(
        self,
        session_id: str,
        shared_root: str = "/data/shared",
        session_root: str = "/data/sessions"
    ):
        self.session_id = session_id
        self.shared_root = shared_root
        self.session_root = session_root
    
    def map_input_path(self, user_path: str) -> str:
        """映射输入文件路径"""
        # 用户上传的文件
        if user_path.startswith("uploads/"):
            return f"{self.session_root}/{self.session_id}/{user_path}"
        # 共享数据集
        if user_path.startswith("datasets/"):
            return f"{self.shared_root}/{user_path}"
        return user_path
    
    def map_output_path(self, user_path: str) -> str:
        """映射输出文件路径"""
        if not user_path.startswith("/"):
            return f"{self.session_root}/{self.session_id}/outputs/{user_path}"
        return user_path
```

---

## 十三、开发路线图

### Phase 1: 基础实现（2 周）

- [ ] Sandbox Worker 实现
  - [ ] IPython Shell 集成
  - [ ] FastAPI 服务端点
  - [ ] 流式输出支持
  - [ ] DataFrame 信息提取
- [ ] Docker 镜像构建
  - [ ] 依赖打包
  - [ ] 安全配置

### Phase 2: 容器管理（2 周）

- [ ] Sandbox Gateway 实现
  - [ ] 容器池管理
  - [ ] 负载均衡
  - [ ] 健康检查
- [ ] SandboxClient SDK
  - [ ] 同步/异步 API
  - [ ] 流式执行支持

### Phase 3: 集成与优化（1 周）

- [ ] TableMind 集成
  - [ ] CodeExecutor 改造
  - [ ] 路径映射
- [ ] 性能优化
  - [ ] 连接池
  - [ ] 缓存策略

### Phase 4: 运维与监控（1 周）

- [ ] 监控指标
- [ ] 日志收集
- [ ] 告警配置
- [ ] 文档完善

---

## 十四、参考资料

### 14.1 开源项目参考

| 项目 | 参考点 | 链接 |
|------|--------|------|
| codebox-api | IPython 执行引擎、流式输出、Docker 容器化 | https://github.com/shroominic/codebox-api |
| ipybox | Jupyter Kernel Gateway、MCP 集成 | https://github.com/gradion-ai/ipybox |
| dify-sandbox | Seccomp 安全机制、Go 服务架构 | https://github.com/langgenius/dify-sandbox |
| microsandbox | microVM 隔离、SDK 设计 | https://github.com/AskTheDev/microsandbox |

### 14.2 技术文档

- [IPython InteractiveShell](https://ipython.readthedocs.io/en/stable/api/generated/IPython.core.interactiveshell.html)
- [Docker Security Best Practices](https://docs.docker.com/engine/security/)
- [FastAPI Streaming Responses](https://fastapi.tiangolo.com/advanced/custom-response/#streamingresponse)

---

## 十五、设计优化建议：返回格式

### 15.1 当前设计的问题

当前 `analyze_data` 工具的流程：
```
LLM 生成 → exec 执行 → 返回 DataFrame → MCP 转换为 markdown/dict
```

如果迁移到沙盒，存在以下问题：
- **序列化开销**：完整 DataFrame 需要序列化传输
- **预览 vs 完整**：沙盒 preview 机制只返回部分数据，但 MCP 需要完整结果
- **两次转换**：沙盒序列化 → 传输 → MCP 再转换，效率低

### 15.2 推荐方案：沙盒内直接输出最终格式

**核心思路**：修改代码生成 prompt，让 LLM 生成的代码直接返回最终格式（dict），而不是 DataFrame。

**修改 `code_gen/python/v1.md` prompt**：

```python
import pandas as pd

def analyze(df: pd.DataFrame) -> dict:
    """
    完成代码，返回值需要组织成 dict 格式：
    {
        "markdown": "...",              # Markdown 表格（用于展示）
        "data": [{...}, {...}, ...],    # 字典数组（结构化数据）
        "shape": [rows, cols],          # 结果形状
        "summary": "分析结论..."         # 可选：分析总结
    }
    """
    # 分析逻辑
    result = df.describe()
    
    # 如果结果超过500行，截断
    if len(result) > 500:
        result = result.head(500)
    
    return {
        "markdown": result.to_markdown(),
        "data": result.to_dict(orient='records'),
        "shape": list(result.shape)
    }
```

**沙盒返回处理**：

```python
# 沙盒内执行后，result 变量是 dict 类型
# 直接作为 JSON 返回，无需 DataFrame 序列化

<result>{
    "success": true,
    "execution_time": 0.5,
    "return_value": {                    # ← 直接是 dict，不是 DataFrame 信息
        "markdown": "| col1 | col2 |\n...",
        "data": [{"col1": 1, "col2": "a"}, ...],
        "shape": [100, 5]
    }
}</result>
```

**MCP Server 使用**：

```python
# pandas_mcp_server.py
result = await sandbox.execute(code=code, result_var="result")

if result.success:
    return_value = result.return_value  # 直接是 dict
    return ToolResult(
        content=return_value["markdown"],
        structured_content={"data": return_value["data"]}
    )
```

### 15.3 两种场景的统一设计

| 场景 | 代码返回值 | 沙盒提取方式 | 说明 |
|------|-----------|-------------|------|
| **数据分析** | `dict` | 提取 result 变量值 | 直接返回最终格式 |
| **表格操作** | 无需返回 | 不提取 | 结果已写入文件 |

```python
# 数据分析：指定 result_var，获取 dict 返回值
result = await sandbox.execute(
    code=analysis_code,
    result_var="result",    # 提取 result 变量
)
final_result = result.return_value  # dict 类型

# 表格操作：不指定 result_var，代码直接写文件
result = await sandbox.execute(
    code=operation_code,
    result_var=None,        # 不提取变量
)
# 结果已写入 output_path
```

### 15.4 优势

1. **性能优化**：避免 DataFrame 序列化，直接传输 JSON
2. **数据完整**：返回完整结果（最多500行），不是 preview
3. **减少转换**：沙盒内一次性完成所有转换
4. **统一接口**：两种场景都用同一个 `/exec` 接口

---

## 十六、容器隔离方案深度分析

本章节详细分析 **Docker in Docker (DinD)**、**Docker outside of Docker (DooD)** 和 **Docker + gVisor** 三种隔离方案，为 TableMind 选择最合适的架构。

### 16.1 方案概述

#### 16.1.1 三种方案的核心区别

| 方案 | 核心原理 | 容器关系 | Docker Daemon 位置 |
|------|---------|---------|-------------------|
| **DinD** | 容器内运行独立 Docker daemon | 父子关系（嵌套） | 外层容器内 |
| **DooD** | 容器挂载宿主机 Docker socket | 兄弟关系（平级） | 宿主机 |
| **gVisor** | 使用用户态内核隔离 | 兄弟关系（平级） | 宿主机 |

#### 16.1.2 架构对比图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         三种隔离方案架构对比                                  │
└─────────────────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
方案A: Docker in Docker (DinD) - 真正的嵌套容器
═══════════════════════════════════════════════════════════════════════════════
┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机                                                                      │
│  ┌─ Docker Daemon (宿主机) ───────────────────────────────────────────────┐ │
│  │                                                                        │ │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │ │
│  │  │  外层容器 (privileged 或 Sysbox)                                  │  │ │
│  │  │  ┌─ Docker Daemon (容器内) ──────────────────────────────────┐  │  │ │
│  │  │  │  ┌───────────┐  ┌───────────┐  ┌───────────┐              │  │  │ │
│  │  │  │  │ Worker-1  │  │ Worker-2  │  │ Worker-3  │  ← 子容器    │  │  │ │
│  │  │  │  │ (子容器)  │  │ (子容器)  │  │ (子容器)  │              │  │  │ │
│  │  │  │  └───────────┘  └───────────┘  └───────────┘              │  │  │ │
│  │  │  └───────────────────────────────────────────────────────────┘  │  │ │
│  │  └─────────────────────────────────────────────────────────────────┘  │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
特点：Worker 是外层容器内 Docker daemon 启动的子容器，完全隔离

═══════════════════════════════════════════════════════════════════════════════
方案B: Docker outside of Docker (DooD) - 兄弟容器
═══════════════════════════════════════════════════════════════════════════════
┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机                                                                      │
│  ┌─ Docker Daemon (宿主机) ───────────────────────────────────────────────┐ │
│  │                                                                        │ │
│  │  ┌─────────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐        │ │
│  │  │  Gateway    │  │ Worker-1  │  │ Worker-2  │  │ Worker-3  │        │ │
│  │  │  挂载       │  │           │  │           │  │           │        │ │
│  │  │  docker.sock│──│ (兄弟容器)│  │ (兄弟容器)│  │ (兄弟容器)│        │ │
│  │  │      │      │  │           │  │           │  │           │        │ │
│  │  └──────┼──────┘  └───────────┘  └───────────┘  └───────────┘        │ │
│  │         │                                                             │ │
│  │         └──────────→ 通过 socket 控制宿主机 Docker 启动 Worker         │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  /var/run/docker.sock                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
特点：Gateway 和 Worker 都是宿主机 Docker 的容器（兄弟关系）

═══════════════════════════════════════════════════════════════════════════════
方案C: Docker + gVisor - 用户态内核隔离
═══════════════════════════════════════════════════════════════════════════════
┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机                                                                      │
│  ┌─ Docker Daemon (runtime: runc + runsc) ────────────────────────────────┐ │
│  │                                                                        │ │
│  │  ┌─────────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐        │ │
│  │  │  Gateway    │  │ Worker-1  │  │ Worker-2  │  │ Worker-3  │        │ │
│  │  │  (runc)     │  │ (runsc)   │  │ (runsc)   │  │ (runsc)   │        │ │
│  │  │             │  │  ┌─────┐  │  │  ┌─────┐  │  │  ┌─────┐  │        │ │
│  │  │             │  │  │Sentry│  │  │  │Sentry│  │  │  │Sentry│  │        │ │
│  │  │             │  │  │用户态│  │  │  │用户态│  │  │  │用户态│  │        │ │
│  │  │             │  │  │内核  │  │  │  │内核  │  │  │  │内核  │  │        │ │
│  │  │             │  │  └─────┘  │  │  └─────┘  │  │  └─────┘  │        │ │
│  │  └─────────────┘  └───────────┘  └───────────┘  └───────────┘        │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
特点：Worker 使用 gVisor 用户态内核，syscall 级隔离
```

### 16.2 方案A：Docker in Docker (DinD) 详细分析

#### 16.2.1 什么是 DinD

**DinD (Docker in Docker)** 是在 Docker 容器内部运行一个**完整独立的 Docker daemon**，形成真正的嵌套容器结构。

```
核心特征：
• 外层容器运行独立的 Docker daemon
• Worker 是外层 daemon 启动的子容器
• 父子关系，完全隔离
• 需要特殊权限（privileged 或 Sysbox）
```

#### 16.2.2 架构设计

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        DinD 架构详细设计                                     │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机                                                                      │
│                                                                             │
│  Docker Daemon (宿主机)                                                      │
│  │                                                                          │
│  └─→ ┌───────────────────────────────────────────────────────────────────┐ │
│      │  Sandbox Manager 容器 (外层，privileged 或 sysbox)                  │ │
│      │                                                                    │ │
│      │  ┌─ Docker Daemon (容器内，独立实例) ───────────────────────────┐  │ │
│      │  │                                                              │  │ │
│      │  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐          │  │ │
│      │  │  │ Worker-1    │  │ Worker-2    │  │ Worker-3    │ ← 子容器 │  │ │
│      │  │  │  IPython    │  │  IPython    │  │  IPython    │          │  │ │
│      │  │  └─────────────┘  └─────────────┘  └─────────────┘          │  │ │
│      │  │                                                              │  │ │
│      │  │  独立的 Docker 网络、存储、镜像缓存                           │  │ │
│      │  └──────────────────────────────────────────────────────────────┘  │ │
│      │                                                                    │ │
│      │  Gateway 服务（管理内层 Docker daemon）                             │ │
│      └───────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│  卷挂载：宿主机 → 外层容器 → 内层容器（两跳）                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.2.3 实现方式

**方式一：Privileged 模式（安全风险高）**
```yaml
# docker-compose.yml
services:
  sandbox-manager:
    image: docker:dind
    privileged: true  # ⚠️ 严重安全风险！容器可访问宿主机所有资源
    volumes:
      - /srv/tablemind/data:/data
```

**方式二：Sysbox（推荐的 DinD 方案）**
```yaml
# 使用 Sysbox 运行时，无需 privileged
services:
  sandbox-manager:
    image: tablemind/sandbox-manager
    runtime: sysbox-runc  # 安全的嵌套容器
    volumes:
      - /srv/tablemind/data:/data
```

#### 16.2.4 优缺点分析

| 维度 | 评价 | 说明 |
|------|------|------|
| **隔离性** | ⭐⭐⭐⭐⭐ | 完全独立的 Docker daemon，彻底隔离 |
| **安全性** | ⭐⭐ (privileged) / ⭐⭐⭐⭐ (Sysbox) | privileged 有严重安全风险 |
| **性能** | ⭐⭐ | 两层容器开销大，内存/CPU 消耗高 |
| **启动速度** | ⭐⭐ | 需先启动内层 Docker daemon + 容器，2-5 秒 |
| **管理复杂度** | ⭐⭐ | 管理两层容器生命周期复杂 |
| **卷挂载** | ⭐⭐ | 跨两层挂载，路径映射复杂 |
| **网络配置** | ⭐⭐ | 多层网络，调试困难 |
| **离线部署** | ⭐⭐ | 内层 Docker 也需要预装镜像 |
| **资源占用** | ⭐⭐ | 每个外层容器额外占用 ~300MB |

#### 16.2.5 适用场景

- ✅ 需要完全隔离的多租户环境（每个租户独立 Docker 环境）
- ✅ CI/CD 场景（构建镜像需要独立 Docker）
- ✅ 已有 Sysbox 基础设施
- ❌ 对启动速度要求高（<500ms）
- ❌ 资源受限环境
- ❌ 简单的代码执行沙盒场景

---

### 16.3 方案B：Docker outside of Docker (DooD) 详细分析

#### 16.3.1 什么是 DooD

**DooD (Docker outside of Docker)** 是在容器内挂载宿主机的 Docker socket (`/var/run/docker.sock`)，让容器内的 Docker client 能够控制**宿主机**的 Docker daemon。

```
核心特征：
• 只有一个 Docker daemon（宿主机的）
• Gateway 容器通过 socket 控制宿主机 Docker
• Worker 与 Gateway 是兄弟容器（同级）
• 无需 privileged，但 socket 挂载有安全风险
```

#### 16.3.2 架构设计

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        DooD 架构详细设计                                     │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机                                                                      │
│                                                                             │
│  Docker Daemon (唯一的 daemon)                                               │
│  │                                                                          │
│  ├─→ ┌─────────────────┐                                                   │
│  │   │  Gateway 容器    │                                                   │
│  │   │                 │                                                   │
│  │   │  挂载 docker.sock ───────────────────────────────────────┐          │
│  │   │  • 负载均衡      │                                       │          │
│  │   │  • 调用 docker   │                                       │          │
│  │   │    API 创建/管理 │                                       │          │
│  │   │    Worker 容器   │                                       │          │
│  │   └─────────────────┘                                       │          │
│  │                                                              │          │
│  │                      ┌───────────────────────────────────────┘          │
│  │                      │ Gateway 通过 socket 控制 Docker daemon            │
│  │                      ▼                                                   │
│  ├─→ ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                    │
│  │   │ Worker-1    │  │ Worker-2    │  │ Worker-3    │  ← 兄弟容器        │
│  │   │  IPython    │  │  IPython    │  │  IPython    │                    │
│  │   └─────────────┘  └─────────────┘  └─────────────┘                    │
│  │                                                                          │
│  └─→ 所有容器由同一个 Docker daemon 管理（平级关系）                          │
│                                                                             │
│  /var/run/docker.sock ← Gateway 挂载此文件                                   │
│  /srv/tablemind/data  ← 所有 Worker 直接挂载                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.3.3 实现方式

```yaml
# docker-compose.yml (DooD 方案)
version: '3.8'

services:
  # Gateway 挂载宿主机 Docker socket
  sandbox-gateway:
    image: tablemind/sandbox-gateway
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # ⚠️ 核心：挂载 socket
      - /srv/tablemind/data:/data
    ports:
      - "8080:8080"
    networks:
      - sandbox-network

  # Worker 由 Gateway 动态创建（不在 compose 中定义）
  # Gateway 通过 Docker API 创建 Worker 容器

networks:
  sandbox-network:
    driver: bridge
```

**Gateway 动态管理 Worker 示例**：

```python
# Gateway 中的容器管理代码
import docker

client = docker.from_env()  # 连接宿主机 Docker daemon

def create_worker():
    container = client.containers.run(
        "tablemind/sandbox-worker",
        detach=True,
        network="sandbox-network",
        volumes={
            "/srv/tablemind/data": {"bind": "/data", "mode": "rw"}
        },
        mem_limit="2g",
        cpu_period=100000,
        cpu_quota=100000,  # 1 CPU
    )
    return container
```

#### 16.3.4 DinD vs DooD 关键区别

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          DinD vs DooD 对比                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  DinD (Docker in Docker)                                                    │
│  ┌─────────────────────────────────────────┐                               │
│  │  外层容器                                │                               │
│  │  ┌─────────────────────────────────┐   │                               │
│  │  │  独立 Docker Daemon              │   │                               │
│  │  │  ┌─────────┐  ┌─────────┐       │   │                               │
│  │  │  │Worker-1 │  │Worker-2 │ 子容器│   │                               │
│  │  │  └─────────┘  └─────────┘       │   │                               │
│  │  └─────────────────────────────────┘   │                               │
│  └─────────────────────────────────────────┘                               │
│  • 真正的嵌套                                                                │
│  • 完全隔离的 Docker 环境                                                    │
│  • 需要 privileged 或 Sysbox                                                │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  DooD (Docker outside of Docker)                                            │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │  宿主机 Docker Daemon                                               │    │
│  │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐               │    │
│  │  │ Gateway │  │Worker-1 │  │Worker-2 │  │Worker-3 │  全部是兄弟   │    │
│  │  │ (挂载   │  │         │  │         │  │         │               │    │
│  │  │ socket) │  │         │  │         │  │         │               │    │
│  │  └────┬────┘  └─────────┘  └─────────┘  └─────────┘               │    │
│  │       │                                                            │    │
│  │       └────→ 通过 socket 创建/管理 Worker                          │    │
│  └────────────────────────────────────────────────────────────────────┘    │
│  • 不是嵌套，是平级                                                         │
│  • 共享宿主机 Docker daemon                                                 │
│  • 无需 privileged（但 socket 挂载有风险）                                  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.3.5 优缺点分析

| 维度 | 评价 | 说明 |
|------|------|------|
| **隔离性** | ⭐⭐⭐ | 容器级隔离，但共享 Docker daemon |
| **安全性** | ⭐⭐⭐ | socket 挂载可控制宿主机所有容器 |
| **性能** | ⭐⭐⭐⭐ | 无嵌套开销，接近原生 |
| **启动速度** | ⭐⭐⭐⭐⭐ | 直接启动容器，<500ms |
| **管理复杂度** | ⭐⭐⭐⭐ | 单层容器，管理简单 |
| **卷挂载** | ⭐⭐⭐⭐⭐ | 直接挂载，无路径映射问题 |
| **网络配置** | ⭐⭐⭐⭐ | 标准 Docker 网络 |
| **离线部署** | ⭐⭐⭐⭐⭐ | 只需宿主机有镜像 |
| **资源占用** | ⭐⭐⭐⭐⭐ | 无额外 daemon 开销 |

#### 16.3.6 安全风险

```
⚠️ DooD 的安全风险：挂载 docker.sock

┌─────────────────────────────────────────────────────────────────────────────┐
│  风险：Gateway 容器拥有宿主机 Docker 的完全控制权                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  恶意代码可以：                                                              │
│  • 创建 privileged 容器                                                      │
│  • 挂载宿主机任意目录                                                        │
│  • 删除其他容器                                                              │
│  • 访问宿主机 Docker 镜像和数据                                              │
│                                                                             │
│  缓解措施：                                                                  │
│  1. Gateway 代码需要严格审计（不执行用户代码）                                │
│  2. Worker 容器不挂载 docker.sock                                           │
│  3. 使用 Docker AuthZ 插件限制 API                                          │
│  4. 使用只读 socket 代理（如 Tecnativa/docker-socket-proxy）                │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.3.7 适用场景

- ✅ 代码执行沙盒（Gateway 可信，Worker 执行不可信代码）
- ✅ 需要动态扩缩容的场景
- ✅ 对启动速度要求高
- ✅ 资源受限环境
- ❌ 多租户需要完全隔离的场景
- ❌ Gateway 本身也执行不可信代码的场景

---

### 16.4 方案C：Docker + gVisor 详细分析

#### 16.4.1 gVisor 工作原理

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         gVisor 架构原理                                      │
└─────────────────────────────────────────────────────────────────────────────┘

传统 Docker 容器：
┌─────────────────────────────────────────────────────────────────────────────┐
│  容器进程  →  系统调用  →  宿主机 Linux 内核  →  硬件                          │
│                              ↑ 共享内核，隔离弱                               │
└─────────────────────────────────────────────────────────────────────────────┘

gVisor 容器：
┌─────────────────────────────────────────────────────────────────────────────┐
│  容器进程  →  系统调用  →  Sentry (用户态内核)  →  有限的宿主机调用  →  硬件   │
│                              ↑ 用户态实现，syscall 过滤                       │
│                                                                             │
│  Sentry：用 Go 实现的用户态 Linux 内核                                        │
│  • 拦截并处理容器的所有系统调用                                                │
│  • 大部分 syscall 在用户态实现                                                │
│  • 仅有限的 syscall 传递到宿主机内核                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.4.2 架构设计

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Docker + gVisor 架构设计                                 │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机 (Linux)                                                              │
│                                                                             │
│  Docker Daemon                                                              │
│  ├── runtime: runc (默认)     ← Gateway 容器使用                            │
│  └── runtime: runsc (gVisor)  ← Worker 容器使用                             │
│                                                                             │
│  ┌─────────────────┐    ┌─────────────────────────────────────────────┐    │
│  │ Gateway 容器     │    │           Worker 容器 (gVisor 隔离)          │    │
│  │ (runtime: runc) │    │                                             │    │
│  │                 │    │  ┌─────────┐  ┌─────────┐  ┌─────────┐     │    │
│  │  • 负载均衡     │    │  │Worker-1 │  │Worker-2 │  │Worker-3 │     │    │
│  │  • 容器池管理   │────│  │ runsc   │  │ runsc   │  │ runsc   │     │    │
│  │  • HTTP 路由    │    │  │         │  │         │  │         │     │    │
│  │                 │    │  │ Sentry  │  │ Sentry  │  │ Sentry  │     │    │
│  └─────────────────┘    │  │ (用户态 │  │ (用户态 │  │ (用户态 │     │    │
│                         │  │  内核)  │  │  内核)  │  │  内核)  │     │    │
│                         │  └─────────┘  └─────────┘  └─────────┘     │    │
│                         └─────────────────────────────────────────────┘    │
│                                           │                                 │
│  /srv/tablemind/data ←────────────────────┘ 卷挂载                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.4.3 配置示例

```bash
# 1. 安装 gVisor (宿主机)
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor.gpg
echo "deb [signed-by=/usr/share/keyrings/gvisor.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list
sudo apt-get update && sudo apt-get install -y runsc

# 2. 配置 Docker 使用 gVisor
cat > /etc/docker/daemon.json <<EOF
{
    "runtimes": {
        "runsc": {
            "path": "/usr/bin/runsc",
            "runtimeArgs": [
                "--network=sandbox",
                "--platform=ptrace"
            ]
        }
    }
}
EOF
sudo systemctl restart docker
```

```yaml
# docker-compose.yml
version: '3.8'

services:
  gateway:
    image: tablemind/sandbox-gateway
    # 默认 runtime (runc)，无需 gVisor
    ports:
      - "8080:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    networks:
      - sandbox-net

  worker-1:
    image: tablemind/sandbox-worker
    runtime: runsc  # 使用 gVisor
    volumes:
      - /srv/tablemind/data:/data
    networks:
      - sandbox-net
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G

networks:
  sandbox-net:
    driver: bridge
```

#### 16.4.4 优缺点分析

| 维度 | 评价 | 说明 |
|------|------|------|
| **隔离性** | ⭐⭐⭐⭐ | 用户态内核，syscall 隔离 |
| **安全性** | ⭐⭐⭐⭐⭐ | 最小攻击面，无需 privileged |
| **性能** | ⭐⭐⭐⭐ | 比 DinD 开销小，部分 syscall 有开销 |
| **启动速度** | ⭐⭐⭐⭐ | 与普通容器接近，约 200-500ms |
| **管理复杂度** | ⭐⭐⭐⭐ | 标准 Docker 命令，配置简单 |
| **卷挂载** | ⭐⭐⭐⭐⭐ | 与普通容器一致 |
| **网络配置** | ⭐⭐⭐⭐ | 标准 Docker 网络 |
| **离线部署** | ⭐⭐⭐⭐ | 只需安装 runsc 二进制 |

#### 16.4.5 兼容性注意事项

**已知兼容的 Python 库**：
- ✅ pandas, numpy, scipy
- ✅ scikit-learn, statsmodels
- ✅ matplotlib (Agg 后端)
- ✅ IPython

**可能有问题的场景**：
- ⚠️ 使用 mmap 的大文件操作（需测试）
- ⚠️ 多进程 (multiprocessing) 某些模式
- ⚠️ 底层网络操作

**TableMind 场景验证**：由于 TableMind 主要使用 pandas 进行数据分析，gVisor 完全兼容。

---

### 16.5 方案对比总结

#### 16.5.1 全维度对比表

| 对比维度 | DinD | DooD | gVisor | 普通 Docker |
|----------|------|------|--------|-------------|
| **安全隔离** | ⭐⭐⭐⭐⭐ 完全隔离 | ⭐⭐⭐ 容器级 | ⭐⭐⭐⭐ syscall 级 | ⭐⭐ 共享内核 |
| **安全风险** | ⭐⭐ (需 privileged) | ⭐⭐⭐ (socket 风险) | ⭐⭐⭐⭐⭐ 最小 | ⭐⭐⭐ |
| **性能开销** | 高 (30-50%) | 低 (接近原生) | 中 (5-15%) | 低 |
| **启动速度** | 慢 (2-5s) | 快 (<500ms) | 快 (200-500ms) | 最快 (<200ms) |
| **内存占用** | 高 (+300MB) | 低 | 中 (+50MB) | 低 |
| **配置复杂度** | 高 | 中 | 中 | 低 |
| **运维成本** | 高 | 中 | 中 | 低 |
| **卷挂载** | 复杂（两跳） | 简单 | 简单 | 简单 |
| **网络配置** | 复杂（多层） | 简单 | 简单 | 简单 |
| **Windows 支持** | ❌ | ✅ | ❌ | ✅ |
| **离线部署** | 复杂 | 简单 | 简单 | 简单 |

#### 16.5.2 三种方案的本质区别

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        三种方案本质区别                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  DinD (Docker in Docker)                                                    │
│  • 核心：嵌套 Docker daemon                                                  │
│  • 隔离层：容器边界 + Docker daemon 边界                                      │
│  • 风险点：需要 privileged 或 Sysbox                                        │
│  • 适用：需要完全独立 Docker 环境的场景                                       │
│                                                                             │
│  DooD (Docker outside of Docker)                                            │
│  • 核心：共享宿主机 Docker daemon                                            │
│  • 隔离层：容器边界（标准 Docker）                                            │
│  • 风险点：socket 挂载可控制宿主机所有容器                                    │
│  • 适用：Gateway 可信、需要动态管理 Worker 的场景                            │
│                                                                             │
│  gVisor                                                                     │
│  • 核心：用户态内核拦截 syscall                                              │
│  • 隔离层：容器边界 + syscall 边界                                           │
│  • 风险点：兼容性（部分 syscall 不支持）                                      │
│  • 适用：执行不可信代码、需要强隔离的场景                                     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.5.3 针对 TableMind 需求的评估

| TableMind 需求 | DinD | DooD | gVisor | 推荐 |
|---------------|------|------|--------|------|
| 高并发 (10+) | ⚠️ 资源消耗大 | ✅ 轻量 | ✅ 轻量 | DooD/gVisor |
| 启动速度 <500ms | ❌ 难实现 | ✅ 可实现 | ✅ 可实现 | DooD/gVisor |
| 离线部署 | ⚠️ 复杂 | ✅ 简单 | ✅ 简单 | DooD/gVisor |
| 文件挂载 | ⚠️ 复杂 | ✅ 简单 | ✅ 简单 | DooD/gVisor |
| 安全隔离 | ✅ 最强 | ⭐⭐⭐ 中等 | ✅ 强 | gVisor |
| 执行不可信代码 | ✅ | ⚠️ Worker 可信 | ✅ | DinD/gVisor |
| 流式输出 | ✅ | ✅ | ✅ | 均可 |
| pandas/numpy 兼容 | ✅ | ✅ | ✅ | 均可 |

---

### 16.6 推荐方案：DooD + gVisor 组合

#### 16.6.1 推荐理由

基于 TableMind 的实际需求，**推荐使用 DooD + gVisor 组合方案**：

- **Gateway 层**：使用 DooD 模式（挂载 docker.sock），负责动态管理 Worker 容器池
- **Worker 层**：使用 gVisor 隔离（runtime=runsc），执行不可信的 LLM 生成代码

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    推荐方案：DooD + gVisor 组合                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  为什么选择 DooD（而不是 DinD）管理 Worker？                                  │
│  ─────────────────────────────────────────────                              │
│  • 无需 privileged 权限                                                      │
│  • 启动速度快（<500ms）                                                      │
│  • 资源占用低（无嵌套 Docker daemon 开销）                                   │
│  • 卷挂载简单（直接挂载宿主机路径）                                           │
│  • Gateway 代码可控可信，socket 风险可接受                                   │
│                                                                             │
│  为什么 Worker 使用 gVisor（而不是普通容器）？                                │
│  ─────────────────────────────────────────────                              │
│  • Worker 执行 LLM 生成的不可信代码                                          │
│  • gVisor 的 syscall 隔离提供额外安全层                                      │
│  • 即使容器逃逸，也被 Sentry 拦截                                            │
│  • 对 pandas/numpy 等数据分析库完全兼容                                      │
│                                                                             │
│  组合优势：                                                                  │
│  ─────────                                                                  │
│  • 管理层（DooD）：简单高效，动态扩缩容                                       │
│  • 执行层（gVisor）：强隔离，防护恶意代码                                     │
│  • 双层防护：即使 Worker 被攻破，也无法控制 Docker daemon                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.6.2 最终架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│              TableMind 沙盒最终架构 (DooD + gVisor)                          │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机 (Linux, 无外网)                                                       │
│                                                                             │
│  Docker Daemon (配置 gVisor runtime: runsc)                                 │
│  │                                                                          │
│  │  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  │                   sandbox-network (internal)                     │    │
│  │  │                                                                  │    │
│  │  │  ┌─────────────────────┐                                        │    │
│  │  │  │  Gateway 容器        │ :8080                                  │    │
│  │  │  │  runtime: runc      │  ← TableMind MCP Server 调用            │    │
│  │  │  │                     │                                        │    │
│  │  │  │  挂载 docker.sock   │ ← DooD 模式                            │    │
│  │  │  │  • 容器池管理       │                                        │    │
│  │  │  │  • 负载均衡         │                                        │    │
│  │  │  │  • 动态创建 Worker  │                                        │    │
│  │  │  └──────────┬──────────┘                                        │    │
│  │  │             │                                                   │    │
│  │  │             │ 通过 Docker API 创建/管理                          │    │
│  │  │             │ (兄弟容器关系)                                     │    │
│  │  │             │                                                   │    │
│  └──│─────────────│───────────────────────────────────────────────────│────│
│     │             ▼                                                   │    │
│     │  ┌──────────────────────────────────────────────────────────┐  │    │
│     │  │              Worker 容器池 (gVisor 隔离)                   │  │    │
│     │  │                                                          │  │    │
│     │  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │  │    │
│     │  │  │ Worker-1 │  │ Worker-2 │  │ Worker-3 │  │ Worker-N │ │  │    │
│     │  │  │ runtime: │  │ runtime: │  │ runtime: │  │ runtime: │ │  │    │
│     │  │  │  runsc   │  │  runsc   │  │  runsc   │  │  runsc   │ │  │    │
│     │  │  │          │  │          │  │          │  │          │ │  │    │
│     │  │  │ ┌──────┐ │  │ ┌──────┐ │  │ ┌──────┐ │  │ ┌──────┐ │ │  │    │
│     │  │  │ │Sentry│ │  │ │Sentry│ │  │ │Sentry│ │  │ │Sentry│ │ │  │    │
│     │  │  │ │用户态│ │  │ │用户态│ │  │ │用户态│ │  │ │用户态│ │ │  │    │
│     │  │  │ │内核  │ │  │ │内核  │ │  │ │内核  │ │  │ │内核  │ │ │  │    │
│     │  │  │ └──────┘ │  │ └──────┘ │  │ └──────┘ │  │ └──────┘ │ │  │    │
│     │  │  │ IPython  │  │ IPython  │  │ IPython  │  │ IPython  │ │  │    │
│     │  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘ │  │    │
│     │  │       │             │             │             │        │  │    │
│     │  └───────┼─────────────┼─────────────┼─────────────┼────────┘  │    │
│     │          │             │             │             │           │    │
│     └──────────┼─────────────┼─────────────┼─────────────┼───────────┘    │
│                │             │             │             │                 │
│                └─────────────┴─────────────┴─────────────┘                 │
│                                     │                                       │
│                           Volume Mount                                      │
│                                     │                                       │
│                                     ▼                                       │
│                ┌───────────────────────────────────────────┐               │
│                │         /srv/tablemind/data               │               │
│                │         宿主机数据目录                      │               │
│                └───────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────────┘

安全边界说明：
┌─────────────────────────────────────────────────────────────────────────────┐
│  第一层：Docker 容器边界                                                      │
│  第二层：gVisor Sentry (用户态内核) ← Worker 额外隔离                         │
│  第三层：sandbox-network (internal) 禁止外网                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### 16.6.3 部署配置更新

```yaml
# docker-compose.yml (更新版)
version: '3.8'

services:
  # Gateway 不需要 gVisor（只做管理）
  sandbox-gateway:
    image: tablemind/sandbox-gateway:latest
    container_name: sandbox-gateway
    # 默认 runtime (runc)
    ports:
      - "8080:8080"
    environment:
      - POOL_MIN_SIZE=3
      - POOL_MAX_SIZE=10
      - WORKER_RUNTIME=runsc  # 通知 Gateway 使用 gVisor 启动 Worker
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    networks:
      - sandbox-network
    restart: unless-stopped

  # Worker 使用 gVisor 隔离
  sandbox-worker-1:
    image: tablemind/sandbox-worker:latest
    container_name: sandbox-worker-1
    runtime: runsc  # ← gVisor 隔离
    volumes:
      - /srv/tablemind/data/shared:/data/shared:ro
      - /srv/tablemind/data/sessions:/data/sessions:rw
    networks:
      - sandbox-network
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G
    restart: unless-stopped

  sandbox-worker-2:
    image: tablemind/sandbox-worker:latest
    container_name: sandbox-worker-2
    runtime: runsc  # ← gVisor 隔离
    volumes:
      - /srv/tablemind/data/shared:/data/shared:ro
      - /srv/tablemind/data/sessions:/data/sessions:rw
    networks:
      - sandbox-network
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G
    restart: unless-stopped

  sandbox-worker-3:
    image: tablemind/sandbox-worker:latest
    container_name: sandbox-worker-3
    runtime: runsc  # ← gVisor 隔离
    volumes:
      - /srv/tablemind/data/shared:/data/shared:ro
      - /srv/tablemind/data/sessions:/data/sessions:rw
    networks:
      - sandbox-network
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2G
    restart: unless-stopped

networks:
  sandbox-network:
    driver: bridge
    internal: true  # 禁止外网访问
```

#### 16.6.4 离线部署步骤

```bash
# 1. 在有网络的环境准备 gVisor
wget https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc
chmod +x runsc

# 2. 打包所需文件
tar -czvf sandbox-offline.tar.gz \
    runsc \
    tablemind-sandbox-gateway.tar \
    tablemind-sandbox-worker.tar \
    docker-compose.yml

# 3. 在离线服务器部署
tar -xzvf sandbox-offline.tar.gz

# 安装 runsc
sudo mv runsc /usr/bin/
sudo chmod +x /usr/bin/runsc

# 配置 Docker
cat > /etc/docker/daemon.json <<EOF
{
    "runtimes": {
        "runsc": {
            "path": "/usr/bin/runsc"
        }
    }
}
EOF
sudo systemctl restart docker

# 加载镜像
docker load < tablemind-sandbox-gateway.tar
docker load < tablemind-sandbox-worker.tar

# 启动服务
docker-compose up -d
```

---

### 16.7 备选方案：何时选择其他方案

#### 16.7.1 何时选择 DinD

虽然推荐 DooD + gVisor，但在以下场景可考虑 DinD：

| 场景 | 原因 |
|------|------|
| **多租户完全隔离** | 每个租户需要完全独立的 Docker 环境 |
| **Worker 需要运行 Docker** | Worker 内部需要启动容器 |
| **已有 Sysbox 基础设施** | 利用现有投资 |
| **监管合规要求** | 某些合规要求完全隔离 |

**DinD + Sysbox 部署示例**：

```yaml
# 如果选择 DinD，推荐使用 Sysbox
services:
  sandbox-manager:
    image: tablemind/sandbox-manager-dind
    runtime: sysbox-runc  # 安全的嵌套容器
    volumes:
      - /srv/tablemind/data:/data
    environment:
      - DOCKER_TLS_CERTDIR=  # 禁用 TLS（内部网络）
```

#### 16.7.2 何时选择纯 DooD（不用 gVisor）

如果满足以下条件，可以不使用 gVisor：

| 场景 | 原因 |
|------|------|
| **执行代码可信** | 代码经过严格审核，不是 LLM 直接生成 |
| **Windows 部署** | gVisor 不支持 Windows |
| **特殊 syscall 需求** | 使用的库需要 gVisor 不支持的 syscall |
| **性能极致要求** | gVisor 5-15% 性能开销无法接受 |

**纯 DooD 部署**：直接使用普通 Docker 容器作为 Worker

```yaml
services:
  sandbox-worker:
    image: tablemind/sandbox-worker
    # 不指定 runtime，使用默认 runc
    security_opt:
      - no-new-privileges:true
      - seccomp:seccomp-profile.json  # 使用 seccomp 代替 gVisor
    cap_drop:
      - ALL
```

---

## 十七、总结

本方案基于对 codebox-api、ipybox、dify-sandbox、microsandbox 四个开源项目的深入分析，为 TableMind 设计了一套完整的 Python 代码执行沙盒环境：

**核心设计决策**：

1. **执行引擎**：采用 IPython InteractiveShell，支持状态保持和丰富的输出格式
2. **统一流式接口**：只有一个 `/exec` 接口，全程流式输出（实时 print）+ 最后返回完整结果
3. **隔离机制**：推荐 DooD + gVisor 组合，管理层简单高效，执行层强隔离
4. **高并发**：容器池预热 + Gateway 负载均衡
5. **文件访问**：Docker 卷挂载，透明访问宿主机数据
6. **离线部署**：所有依赖打包到镜像 + runsc 二进制，无需外网
7. **返回格式优化**：数据分析代码直接返回 dict（含 markdown/data），避免 DataFrame 序列化

**隔离方案选择**：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          三种方案对比                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  DinD (嵌套容器)          DooD (兄弟容器)          gVisor (syscall隔离)      │
│  ├─ 隔离性：⭐⭐⭐⭐⭐      ├─ 隔离性：⭐⭐⭐           ├─ 隔离性：⭐⭐⭐⭐        │
│  ├─ 安全性：⭐⭐           ├─ 安全性：⭐⭐⭐           ├─ 安全性：⭐⭐⭐⭐⭐      │
│  ├─ 性能：⭐⭐ 高开销      ├─ 性能：⭐⭐⭐⭐⭐ 原生    ├─ 性能：⭐⭐⭐⭐          │
│  ├─ 启动：2-5s            ├─ 启动：<500ms           ├─ 启动：200-500ms       │
│  └─ 需 privileged         └─ socket 有风险         └─ 兼容性需验证          │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  推荐方案：DooD + gVisor 组合                                                │
│  ─────────────────────────                                                  │
│  • Gateway 层：DooD 模式管理 Worker（简单高效，动态扩缩容）                   │
│  • Worker 层：gVisor 隔离执行代码（强隔离，防护恶意代码）                     │
│                                                                             │
│  优势：                                                                      │
│  • 无需 privileged 权限                                                      │
│  • 启动速度快（<500ms）                                                      │
│  • pandas/numpy 完全兼容                                                     │
│  • 双层防护：Worker 即使被攻破也无法控制 Docker daemon                       │
│  • 离线部署简单（只需 runsc 二进制）                                          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

**接口设计亮点**：

```
统一流式接口 POST /exec：
├── 执行过程中 → 实时推送 <txt>/<err>/<img>（用户能看到执行进度）
└── 执行结束后 → 推送 <result>{...}（用户获取完整结果）

result_var 参数控制提取方式：
├── 数据分析场景：result_var="result" → 提取 dict 返回值（含 markdown/data）
└── 表格操作场景：result_var=None    → 不提取（结果已写入文件）

优势：
• 一个接口满足所有需求
• 流式输出与结果返回统一
• 数据分析直接返回最终格式，无需二次转换
• 表格操作直接写文件，无需返回数据
```

**最终架构（DooD + gVisor）**：

```
┌─────────────────────────────────────────────────────────────────┐
│  TableMind MCP Server                                           │
└───────────────────────────┬─────────────────────────────────────┘
                            │ HTTP (SSE)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  Gateway 容器 (runtime: runc, 挂载 docker.sock)  ← DooD 模式    │
│  • 负载均衡 + 容器池管理                                          │
│  • 通过 Docker API 动态创建/管理 Worker                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │ Docker API (兄弟容器)
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌───────────────┐   ┌───────────────┐   ┌───────────────┐
│ Worker-1      │   │ Worker-2      │   │ Worker-N      │
│ runtime:runsc │   │ runtime:runsc │   │ runtime:runsc │  ← gVisor
│ (gVisor)      │   │ (gVisor)      │   │ (gVisor)      │    隔离
│ ┌───────────┐ │   │ ┌───────────┐ │   │ ┌───────────┐ │
│ │  Sentry   │ │   │ │  Sentry   │ │   │ │  Sentry   │ │
│ │ 用户态内核│ │   │ │ 用户态内核│ │   │ │ 用户态内核│ │
│ └───────────┘ │   │ └───────────┘ │   │ └───────────┘ │
└───────┬───────┘   └───────┬───────┘   └───────┬───────┘
        │                   │                   │
        └───────────────────┴───────────────────┘
                            │ Volume Mount
                            ▼
              /srv/tablemind/data (宿主机)
```

**预期效果**：

- 代码执行安全隔离（gVisor syscall 级隔离 + DooD 容器边界），不影响主服务
- 支持 10+ 并发分析请求
- 执行延迟 < 500ms（预热容器 + gVisor 快速启动）
- 流式输出实时反馈（print 内容实时显示）
- 数据分析返回完整结果（最多500行），直接可用
- 离线部署简单，无外网依赖
- 双层防护：即使 Worker 被攻破，也无法控制 Docker daemon


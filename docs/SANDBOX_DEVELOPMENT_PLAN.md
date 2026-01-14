# TableMind 沙盒系统 - 渐进式开发计划

> 本文档是 `SANDBOX_FINAL_DESIGN.md` 的配套开发计划，将最终设计拆解为 5 个可渐进式开发的阶段。

## 总体路线图

```
阶段 1: Worker 核心开发 ─────────────────────────────────────────┐
        (单体 FastAPI + IPython 状态保持)                         │
                    ↓                                              │
阶段 2: SandboxManager 基础版 ──────────────────────────────────│
        (本地 Docker 编排 + HTTP 通信)                            │  核心逻辑
                    ↓                                              │  不变
阶段 3: DinD 架构集成 ──────────────────────────────────────────│
        (将 Manager 放入容器 + 级联挂载)                          │
                    ↓                                              │
阶段 4: 安全与增强 ─────────────────────────────────────────────┘
        (引入 gVisor + 容器池预热)
                    ↓
阶段 5: 业务集成与离线包
        (接入 MCP + 打包)
```

## 时间估算

| 阶段 | 阶段名称 | 核心任务描述 | 预计耗时 |
|------|----------|-------------|---------|
| 阶段 1 | Worker 核心开发 | IPython 封装、FastAPI + SSE、变量序列化、脏变量清理 | 2 天 |
| 阶段 2 | SandboxManager 基础版 | docker-py 集成、Session 管理、HTTP 转发、文件挂载验证 | 2 天 |
| 阶段 3 | DinD 架构集成 | TableMind 镜像构建、docker-compose 编排、级联挂载调试 | 3 天 |
| 阶段 4 | 安全与增强 | gVisor 集成、容器池、资源限制、健康检查 | 3 天 |
| 阶段 5 | 业务集成与离线包 | MCP 工具改造、离线镜像加载、最终打包 | 2 天 |
| **总计** | | | **12 天** |

---

## 阶段 1: Worker 核心开发

### 1.1 目标

**不涉及 Docker**，只开发 Worker 的核心代码。确保 Python 代码能够通过 HTTP 接口执行，并且状态（变量）能跨请求保留。

### 1.2 为什么是基石

这是整个系统的基石，后续阶段只是在这个基石外面套壳（Docker/gVisor）：
- 阶段 2-5 的改动都在 **基础设施层**，不会修改 Worker 内部的执行逻辑
- 解决最难的 IPython 输出捕获和变量序列化问题
- Worker 代码在阶段 1 完成后基本定型

### 1.3 关键设计决策

| 决策点 | 选择 | 原因 |
|--------|------|------|
| Session 管理 | 单 Session | 阶段 2 后每个容器就是一个 Session，Worker 无需管理多 Session |
| 输出捕获 | 自定义 stdout/stderr 拦截 | IPython 原生输出不易获取 |
| 流式协议 | SSE (Server-Sent Events) | 实时推送执行输出，比 WebSocket 简单 |

### 1.4 任务清单

#### 任务 1.1: IPython 引擎封装

**目标**: 封装 `InteractiveShell`，提供稳定的代码执行接口

**具体步骤**:
1. 创建 `worker/core/executor.py`
2. 实现 `IPythonExecutor` 类
   - `__init__()`: 创建 InteractiveShell 实例，初始化 user_ns
   - `run_code(code: str)`: 执行代码，返回结果
   - `get_variable(name: str)`: 从 user_ns 获取变量
   - `reset()`: 重置 Shell 状态（清空 user_ns）

**验证点**:
- [x] 多次调用 `run_code()`，变量在 `user_ns` 中持久化
- [x] `import pandas as pd` 后，后续代码可以使用 `pd`
- [x] `df = pd.DataFrame(...)` 后，后续代码可以使用 `df`

**注意事项**:
- IPython 的 `InteractiveShell.instance()` 是单例，需要直接实例化 `InteractiveShell()`
- 注意 `user_ns` 和 `user_global_ns` 的区别

---

#### 任务 1.2: 输出捕获机制

**目标**: 实时捕获 `print()` 输出、异常信息，支持流式推送

**具体步骤**:
1. 创建 `worker/core/output_capture.py`
2. 实现 `OutputCapture` 类
   - 自定义 `sys.stdout` 和 `sys.stderr` 的替换类
   - 使用 `asyncio.Queue` 存储捕获的输出片段
   - 区分输出类型：`text` (stdout)、`error` (stderr)
3. 在 `IPythonExecutor.run_code()` 中使用 `OutputCapture` 上下文管理器

**输出格式** (SSE 数据):
```
<txt>print 输出内容</txt>
<err>错误信息</err>
<img>base64 编码图片</img>
<result>{"success": true, "execution_time": 0.234, "return_value": {...}}</result>
```

**验证点**:
- [x] `print("hello")` 的输出被捕获为 `<txt>hello</txt>`
- [x] 语法错误被捕获为 `<err>...</err>`
- [x] 运行时异常被捕获为 `<err>...</err>`

**注意事项**:
- 需要处理 IPython 自身的异常格式化（traceback 美化）
- 考虑多线程安全（虽然当前是单 Session，但要预留扩展性）

---

#### 任务 1.3: FastAPI 接口开发

**目标**: 暴露 HTTP 接口，实现 SSE 流式输出

**具体步骤**:
1. 创建 `worker/main.py` - FastAPI 应用入口
2. 实现接口：
   - `POST /exec` - 执行代码，SSE 流式返回
   - `POST /reset` - 重置 IPython 状态
   - `GET /health` - 健康检查
3. 实现 SSE 流式响应
   - 使用 `StreamingResponse` + `async generator`
   - 在执行过程中实时推送 `<txt>`、`<err>` 片段
   - 执行结束后推送 `<result>` 片段

**请求格式**:
```json
{
    "code": "import pandas as pd\ndf = pd.read_csv('/data/file.csv')",
    "result_var": "df"
}
```

**响应格式** (SSE 流):
```
data: <txt>Loading data...</txt>

data: <result>{"success": true, "execution_time": 0.234, "return_value": {...}}</result>
```

**验证点**:
- [x] `POST /exec` 返回 `text/event-stream` 类型
- [x] 长时间执行的代码，输出实时推送（不是等执行完才返回）
- [x] `/health` 返回 200

---

#### 任务 1.4: 变量序列化

**目标**: 将 Python 对象序列化为 JSON，用于返回给客户端

**具体步骤**:
1. 创建 `worker/core/serializer.py`
2. 实现 `serialize_variable(var, name: str) -> dict`
3. 支持的类型及序列化内容：

| 类型 | 序列化内容 |
|------|------------|
| DataFrame | type, shape, columns, dtypes, preview (前10行), markdown |
| Series | type, name, dtype, length, data (前100项) |
| dict | type, keys, data (限制大小) |
| list | type, length, data (前100项) |
| 数值/字符串 | type, value |
| 其他 | type, repr (前1000字符) |

**验证点**:
- [x] DataFrame 序列化包含 shape、columns、preview
- [x] 大型 DataFrame (100万行) 不会导致内存溢出
- [x] 嵌套 dict 能正确序列化

---

#### 任务 1.5: 脏变量清理机制

**目标**: 代码执行失败时，回滚新产生的变量，保持 Session 状态一致

**具体步骤**:
1. 在 `IPythonExecutor.run_code()` 中实现：
   - 执行前记录 `keys_before = set(user_ns.keys())`
   - 执行代码
   - 如果执行失败：
     - 计算 `new_keys = set(user_ns.keys()) - keys_before`
     - 删除 `new_keys` 中的所有变量
2. 在 `/exec` 响应中标记执行是否成功

**场景示例**:
```
步骤 1: import pandas as pd       ✅ 成功，pd 保留
步骤 2: df = pd.read_csv(...)     ✅ 成功，df 保留  
步骤 3: temp = df.xxx(); out = ...  ❌ 报错

自动清理：删除 temp（如果已创建）
当前状态：pd, df 保留，可以重新执行修正后的步骤 3
```

**验证点**:
- [x] 执行失败后，新变量被清理
- [x] 执行失败后，之前的变量仍然存在
- [x] 部分成功的代码（多行，前几行成功，后面失败），正确回滚

---

### 1.5 项目结构 (阶段 1 产出)

```
worker/
├── main.py                 # FastAPI 入口
├── requirements.txt        # 依赖: fastapi, uvicorn, ipython, pandas
├── Dockerfile              # 构建 Worker 镜像（暂不发布）
└── core/
    ├── __init__.py
    ├── executor.py         # IPython 执行引擎
    ├── output_capture.py   # 输出捕获
    └── serializer.py       # 变量序列化
```

### 1.6 验证方式

1. **本地运行**:
   ```bash
   cd worker
   pip install -r requirements.txt
   python main.py  # 启动在 :9000
   ```

2. **Postman/curl 测试**:
   ```bash
   # 第一次请求
   curl -X POST http://localhost:9000/exec \
     -H "Content-Type: application/json" \
     -d '{"code": "import pandas as pd\ndf = pd.DataFrame({\"a\": [1,2,3]})"}'
   
   # 第二次请求 - 验证状态保持
   curl -X POST http://localhost:9000/exec \
     -H "Content-Type: application/json" \
     -d '{"code": "print(df.sum())", "result_var": "df"}'
   ```

3. **验证清单**:
   - [x] 变量跨请求保持
   - [x] SSE 流式输出正常
   - [x] 错误能正确捕获
   - [x] 脏变量能正确清理
   - [x] DataFrame 能正确序列化

### 1.7 进入阶段 2 的条件

- [x] 所有任务完成
- [x] 本地测试通过
- [x] Worker Docker 镜像构建成功 (`docker build -t tablemind/worker:latest .`)

---

## 阶段 2: SandboxManager 基础版

### 2.1 目标

开发宿主机上的 Python 控制代码。使用 **宿主机的 Docker Daemon** 来管理阶段 1 产生的 Worker 镜像。

### 2.2 关键设计决策

| 决策点 | 选择 | 原因 |
|--------|------|------|
| Docker 连接 | 宿主机 docker.sock | 开发调试方便，阶段 3 再改为 DinD |
| 容器运行时 | runc (默认) | 降低调试难度，阶段 4 再加 gVisor |
| 容器池 | 不预热 | 先跑通流程，阶段 4 再优化 |
| Session 模型 | 1 Session = 1 Worker 容器 | 符合最终设计，隔离性好 |

### 2.3 架构图 (阶段 2)

```
┌─────────────────────────────────────────────────────────────────┐
│  宿主机                                                          │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  SandboxManager (Python 进程)                            │   │
│  │  ├── 连接宿主机 docker.sock                              │   │
│  │  ├── Session 管理 (session_id -> container_id 映射)      │   │
│  │  └── HTTP 转发 (请求 -> Worker 容器)                     │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              │ HTTP                             │
│                              ↓                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  Worker-1   │  │  Worker-2   │  │  Worker-N   │             │
│  │  :9000      │  │  :9000      │  │  :9000      │             │
│  │  Session:A  │  │  Session:B  │  │  (按需创建) │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
│                                                                 │
│  挂载: /host/test_data → Worker /data                           │
└─────────────────────────────────────────────────────────────────┘
```

### 2.4 任务清单

#### 任务 2.1: Docker Client 封装

**目标**: 使用 docker-py 管理 Worker 容器生命周期

**具体步骤**:
1. 创建 `src/sandbox/docker_client.py`
2. 实现 `DockerManager` 类：
   - `__init__()`: 连接宿主机 docker.sock
   - `create_container(image, volumes) -> container_id`
   - `start_container(container_id)`
   - `get_container_ip(container_id) -> str`
   - `stop_container(container_id)`
   - `remove_container(container_id)`
3. 配置 Worker 容器参数：
   - 不暴露端口（使用容器 IP 直连）
   - 内存限制: 2GB
   - 网络: bridge (默认)

**验证点**:
- [ ] 能启动 Worker 容器
- [ ] 能获取容器 IP
- [ ] 能停止和删除容器

**注意事项**:
- 暂不配置 gVisor (`runtime='runsc'`)，使用默认 runc
- 暂不配置复杂的资源限制

---

#### 任务 2.2: Session 管理

**目标**: 建立 Session ID 与 Worker 容器的映射关系

**具体步骤**:
1. 创建 `src/sandbox/session.py`
2. 实现 `SessionManager` 类：
   - `sessions: Dict[str, SessionInfo]` - 存储映射
   - `SessionInfo` 数据类：session_id, container_id, container_ip, created_at, last_used_at
3. 实现方法：
   - `create_session() -> session_id`
   - `get_session(session_id) -> SessionInfo`
   - `release_session(session_id)`

**验证点**:
- [ ] 创建 Session 返回唯一 ID
- [ ] 能通过 Session ID 获取容器信息
- [ ] 释放 Session 后容器被删除

---

#### 任务 2.3: SandboxManager 主类

**目标**: 整合 Docker 管理和 Session 管理，提供统一接口

**具体步骤**:
1. 创建 `src/sandbox/manager.py`
2. 实现 `SandboxManager` 类：
   - `create_session() -> str`
     1. 启动 Worker 容器
     2. 等待健康检查通过 (`GET /health`)
     3. 创建 Session 映射
     4. 返回 session_id
   - `execute(session_id, code, result_var) -> AsyncIterator[str]`
     1. 根据 session_id 获取容器 IP
     2. 转发 HTTP 请求到 `http://{container_ip}:9000/exec`
     3. 透传 SSE 流
   - `release_session(session_id)`
     1. 停止并删除容器
     2. 清理 Session 映射

**验证点**:
- [ ] `create_session()` 后 `docker ps` 能看到新容器
- [ ] `execute()` 能返回正确结果
- [ ] `release_session()` 后容器被清理

---

#### 任务 2.4: HTTP 转发与 SSE 透传

**目标**: 将客户端请求转发到 Worker，透传 SSE 流

**具体步骤**:
1. 使用 `httpx` 库进行异步 HTTP 请求
2. 实现 SSE 流透传：
   ```python
   async def execute(...) -> AsyncIterator[str]:
       async with httpx.AsyncClient() as client:
           timeout = httpx.Timeout(None)  # 禁用读超时，或者设置一个很大的值（如 execution_timeout）           
           async with client.stream("POST", url, json=payload, timeout=timeout) as resp:
               async for line in resp.aiter_lines():
                   if line.startswith("data: "):
                       yield line[6:]
   ```

**验证点**:
- [ ] 长时间执行的代码，输出实时透传
- [ ] 网络超时有合理的错误处理

---

#### 任务 2.5: 文件挂载验证

**目标**: 验证宿主机文件能被 Worker 读取

**具体步骤**:
1. 在宿主机创建测试目录和文件：
   ```bash
   mkdir -p /tmp/test_data
   echo "a,b,c\n1,2,3" > /tmp/test_data/test.csv
   ```
2. 配置 Volume 挂载：
   ```python
   volumes = {'/tmp/test_data': {'bind': '/data', 'mode': 'ro'}}
   ```
3. 测试代码：
   ```python
   code = "import pandas as pd; df = pd.read_csv('/data/test.csv'); print(df)"
   ```

**验证点**:
- [ ] Worker 能读取挂载的文件
- [ ] 只读模式下 Worker 无法写入

---

### 2.5 项目结构 (阶段 2 新增)

```
TableMind/
├── src/
│   ├── sandbox/
│   │   ├── __init__.py
│   │   ├── manager.py          # SandboxManager 主类
│   │   ├── session.py          # Session 管理
│   │   ├── docker_client.py    # Docker 操作封装
│   │   └── models.py           # 数据模型 (SessionInfo 等)
│   └── ...
└── worker/                      # 阶段 1 产物，不修改
    └── ...
```

### 2.6 验证方式

1. **启动 Worker 镜像** (阶段 1 产物):
   ```bash
   cd worker
   docker build -t tablemind/worker:latest .
   ```

2. **测试 SandboxManager**:
   ```python
   # test_sandbox.py
   import asyncio
   from src.sandbox.manager import SandboxManager
   
   async def main():
       manager = SandboxManager()
       
       # 创建 Session
       session_id = await manager.create_session()
       print(f"Created session: {session_id}")
       
       # 执行代码
       async for chunk in manager.execute(session_id, "import pandas as pd"):
           print(chunk)
       
       async for chunk in manager.execute(session_id, "df = pd.DataFrame({'a': [1,2,3]})"):
           print(chunk)
       
       async for chunk in manager.execute(session_id, "print(df.sum())", "df"):
           print(chunk)
       
       # 释放 Session
       await manager.release_session(session_id)
   
   asyncio.run(main())
   ```

3. **观察容器状态**:
   ```bash
   # 创建 Session 后
   docker ps  # 应该看到 Worker 容器
   
   # 释放 Session 后
   docker ps  # Worker 容器应该消失
   ```

### 2.7 平滑过渡点

此阶段的代码逻辑在迁移到阶段 3 (DinD) 时，**只需修改一处**：

```python
# 阶段 2: 连接宿主机 Docker
docker_client = docker.from_env()

# 阶段 3: 连接内部 Docker (改这一行即可)
docker_client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
```

其他代码（Session 管理、HTTP 转发、SSE 透传）完全不变。

### 2.8 进入阶段 3 的条件

- [ ] 所有任务完成
- [ ] 多 Session 并发测试通过
- [ ] 文件挂载验证通过
- [ ] Session 释放后容器正确清理

---

## 阶段 3: DinD 架构集成

### 3.1 目标

将阶段 2 的 Manager 代码放入 TableMind 容器中，实现 **Docker in Docker**。

### 3.2 为什么是最复杂的阶段

这是架构改造阶段，主要解决两个问题：
1. **网络连通性**: Manager (在 TableMind 容器内) 如何访问 Worker (在子容器内)
2. **级联挂载**: 宿主机文件如何层层传递到 Worker

### 3.3 架构图 (阶段 3)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  宿主机                                                                      │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  TableMind 容器 (privileged: true)                                   │   │
│  │                                                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────┐    │   │
│  │  │  内部 Docker Daemon (dockerd)                                │    │   │
│  │  │  └── 通过 /var/run/docker.sock 暴露                          │    │   │
│  │  └─────────────────────────────────────────────────────────────┘    │   │
│  │                                                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────┐    │   │
│  │  │  SandboxManager (Python 进程)                                │    │   │
│  │  │  └── 连接内部 docker.sock                                    │    │   │
│  │  └─────────────────────────────────────────────────────────────┘    │   │
│  │                              │ HTTP (内部网络)                       │   │
│  │                              ↓                                       │   │
│  │  ┌─────────────────────────────────────────────────────────────┐    │   │
│  │  │  Worker 容器 (由内部 Docker 创建)                            │    │   │
│  │  │  ┌─────────────┐  ┌─────────────┐                           │    │   │
│  │  │  │  Worker-1   │  │  Worker-2   │                           │    │   │
│  │  │  └─────────────┘  └─────────────┘                           │    │   │
│  │  └─────────────────────────────────────────────────────────────┘    │   │
│  │                                                                      │   │
│  │  挂载: /data (来自宿主机) → Worker /data (级联挂载)                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  挂载: 宿主机 /your/data → TableMind /data                                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.4 任务清单

#### 任务 3.1: 构建 TableMind 镜像 (DinD)

**目标**: 创建包含 Docker Daemon 的 TableMind 镜像

**具体步骤**:
1. 创建 `Dockerfile` (项目根目录)
2. 基础镜像选择：
   - 方案 A: `docker:24-dind` (官方 DinD 镜像，基于 Alpine)
   - 方案 B: `python:3.10-slim` + 手动安装 Docker
   - **推荐方案 A**，更稳定
3. 安装 Python 环境和依赖
4. 复制应用代码
5. 配置 entrypoint.sh

**Dockerfile 要点**:
```dockerfile
FROM docker:24-dind

# 安装 Python
RUN apk add --no-cache python3 py3-pip

# 复制应用
COPY requirements.txt /app/
RUN pip install -r /app/requirements.txt
COPY src/ /app/src/

# 入口脚本
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
```

**验证点**:
- [ ] 镜像构建成功
- [ ] 容器内 `docker info` 正常工作

---

#### 任务 3.2: 编写 entrypoint.sh

**目标**: 容器启动时初始化内部 Docker Daemon

**具体步骤**:
1. 创建 `deploy/entrypoint.sh`
2. 实现启动流程：
   1. 启动 dockerd (后台运行)
   2. 等待 dockerd 就绪
   3. 加载离线 Worker 镜像 (如果存在)
   4. 启动主应用

**entrypoint.sh 要点**:
```bash
#!/bin/bash
set -e

# 1. 启动内部 Docker Daemon
dockerd > /var/log/dockerd.log 2>&1 &

# 2. 等待 Docker 就绪
echo "Waiting for internal Docker Daemon..."
until docker info > /dev/null 2>&1; do
    sleep 1
done
echo "Internal Docker Daemon is ready"

# 3. 加载 Worker 镜像（离线场景）
if [ -f "/offline_images/worker.tar" ]; then
    echo "Loading Worker image..."
    docker load -i /offline_images/worker.tar
fi

# 4. 启动主程序
exec python /app/src/main.py
```

**验证点**:
- [ ] 容器启动后 dockerd 正常运行
- [ ] `docker images` 能看到已加载的镜像

---

#### 任务 3.3: 配置 docker-compose.yml

**目标**: 编排 TableMind 服务，配置必要的挂载和权限

**具体步骤**:
1. 创建 `deploy/docker-compose.yml`
2. 配置关键项：
   - `privileged: true` - DinD 必需
   - `init: true` - 使用 tini 作为 init 进程
   - 卷挂载：
     - 宿主机数据 → `/data`
     - 离线镜像 → `/offline_images`
     - 内部 Docker 存储 → `/var/lib/docker`

**docker-compose.yml 要点**:
```yaml
name: tablemind

services:
  tablemind:
    image: tablemind:1.0
    privileged: true
    init: true
    ports:
      - "17414:17414"
    volumes:
      - /your/data/path:/data:ro           # 级联挂载源
      - ./offline_images:/offline_images:ro # 离线镜像
      - tablemind-docker:/var/lib/docker   # 内部 Docker 存储

volumes:
  tablemind-docker:
```

**验证点**:
- [ ] `docker-compose up` 成功启动
- [ ] 进入容器后 `docker ps` 正常（空列表）

---

#### 任务 3.4: 适配 SandboxManager

**目标**: 修改 SandboxManager 以适应 DinD 环境

**具体改动**:

1. **Docker 连接** (唯一代码改动):
   ```python
   # 阶段 2
   docker_client = docker.from_env()
   
   # 阶段 3 - 连接内部 Docker
   docker_client = docker.DockerClient(
       base_url="unix:///var/run/docker.sock"
   )
   ```

2. **网络配置**:
   - 创建内部网络: `docker network create tablemind-internal`
   - Worker 容器加入该网络
   - Manager 通过容器名或 IP 访问 Worker

3. **卷挂载路径**:
   ```python
   # 关键：使用容器内路径，不是宿主机路径！
   volumes = {'/data': {'bind': '/data', 'mode': 'ro'}}
   ```

**验证点**:
- [ ] Manager 能创建 Worker 容器
- [ ] Manager 能访问 Worker 的 HTTP 接口
- [ ] Worker 能读取 /data 下的文件

---

#### 任务 3.5: 调试级联挂载

**目标**: 验证文件从宿主机 → TableMind → Worker 的完整路径

**调试步骤**:
1. 在宿主机创建测试文件:
   ```bash
   mkdir -p /tmp/test_data
   echo "a,b,c\n1,2,3" > /tmp/test_data/test.csv
   ```
2. 修改 docker-compose.yml:
   ```yaml
   volumes:
     - /tmp/test_data:/data:ro
   ```
3. 启动并进入 TableMind 容器:
   ```bash
   docker-compose up -d
   docker exec -it tablemind bash
   
   # 验证 TableMind 能看到文件
   ls /data/test.csv
   ```
4. 在 TableMind 内创建 Worker:
   ```bash
   docker run -v /data:/data:ro tablemind/worker ls /data/
   ```
5. 测试完整流程:
   ```python
   code = "import pandas as pd; print(pd.read_csv('/data/test.csv'))"
   ```

**常见问题**:
- 挂载路径错误（使用了宿主机路径而非容器内路径）
- 权限问题（确保 Worker 用户能读取文件）

**验证点**:
- [ ] 宿主机文件在 Worker 中可读
- [ ] 多个 Worker 能同时读取同一文件

---

#### 任务 3.6: 内部网络调试

**目标**: 确保 Manager 能通过网络访问 Worker

**调试步骤**:
1. 创建内部网络:
   ```bash
   # 在 entrypoint.sh 中添加
   docker network create tablemind-internal 2>/dev/null || true
   ```
2. Worker 加入网络:
   ```python
   container = docker_client.containers.run(
       ...,
       network="tablemind-internal",
       name=f"worker-{session_id[:8]}"
   )
   ```
3. 访问方式：
   - 方式 A: 使用容器 IP (`container.attrs['NetworkSettings']['IPAddress']`)
   - 方式 B: 使用容器名 (`http://worker-xxx:9000`)

**验证点**:
- [ ] Manager 能 ping 通 Worker
- [ ] HTTP 请求能正常到达

---

### 3.5 项目结构 (阶段 3 新增)

```
TableMind/
├── Dockerfile              # TableMind DinD 镜像
├── deploy/
│   ├── docker-compose.yml  # 编排配置
│   ├── entrypoint.sh       # 启动脚本
│   └── daemon.json         # 内部 Docker 配置（可选）
├── src/
│   ├── sandbox/
│   │   └── ...             # 小幅修改 (Docker 连接方式)
│   └── ...
└── worker/
    └── ...                 # 不修改
```

### 3.6 验证方式

1. **构建镜像**:
   ```bash
   docker build -t tablemind:1.0 .
   cd worker && docker build -t tablemind/worker:latest .
   ```

2. **启动服务**:
   ```bash
   docker-compose up -d
   docker-compose logs -f  # 观察启动日志
   ```

3. **进入容器调试**:
   ```bash
   docker exec -it tablemind bash
   docker ps  # 查看 Worker 容器
   docker network ls  # 查看网络
   ```

4. **集成测试**:
   - 调用 MCP 接口或直接调用 SandboxManager
   - 验证代码执行、状态保持、文件读取

### 3.7 进入阶段 4 的条件

- [ ] DinD 架构正常运行
- [ ] 级联挂载验证通过
- [ ] 网络连通性验证通过
- [ ] 多 Session 并发测试通过

---

## 阶段 4: 安全与增强

### 4.1 目标

在架构跑通后，引入 **gVisor 安全隔离** 和 **容器池优化**。

### 4.2 关键设计决策

| 决策点 | 选择 | 原因 |
|--------|------|------|
| 安全运行时 | gVisor (runsc) | 用户态内核，syscall 拦截 |
| gVisor 安装位置 | 打包在镜像内 | 客户无需安装 |
| 降级策略 | 自动检测，不可用时降级为 runc | 兼容性保证 |

### 4.3 任务清单

#### 任务 4.1: gVisor 集成

**目标**: 在 TableMind 镜像中集成 gVisor，Worker 使用 runsc 运行时

**具体步骤**:
1. 修改 Dockerfile，下载 runsc 二进制:
   ```dockerfile
   # 下载 gVisor
   RUN wget -O /usr/bin/runsc https://storage.googleapis.com/gvisor/releases/release/latest/x86_64/runsc \
       && chmod +x /usr/bin/runsc
   ```
2. 创建 `deploy/daemon.json`:
   ```json
   {
       "runtimes": {
           "runsc": {
               "path": "/usr/bin/runsc",
               "runtimeArgs": ["--platform=ptrace"]
           }
       },
       "storage-driver": "vfs"
   }
   ```
3. 修改 entrypoint.sh，复制配置:
   ```bash
   cp /app/deploy/daemon.json /etc/docker/daemon.json
   ```
4. 修改 SandboxManager，指定运行时:
   ```python
   container = docker_client.containers.run(
       ...,
       runtime="runsc"  # 使用 gVisor
   )
   ```

**验证点**:
- [ ] `docker info | grep runsc` 显示运行时可用
- [ ] Worker 容器使用 runsc 运行
- [ ] 代码执行正常（gVisor 兼容性）

---

#### 任务 4.2: gVisor 降级机制

**目标**: gVisor 不可用时自动降级为 runc

**具体步骤**:
1. 在 SandboxManager 初始化时检测:
   ```python
   def _check_gvisor_available(self) -> bool:
       try:
           self.docker_client.containers.run(
               "alpine", "echo hello",
               runtime="runsc",
               remove=True
           )
           return True
       except:
           return False
   ```
2. 根据检测结果选择运行时:
   ```python
   self.runtime = "runsc" if self._gvisor_available else "runc"
   ```
3. 启动日志提示:
   ```
   ✅ gVisor runtime is available
   # 或
   ⚠️ gVisor runtime not detected, using default runtime
   ```

**验证点**:
- [ ] gVisor 可用时使用 runsc
- [ ] gVisor 不可用时降级为 runc
- [ ] 降级后功能正常

---

#### 任务 4.3: 容器池实现

**目标**: 预热 Worker 容器，减少 Session 创建延迟

**具体步骤**:
1. 创建 `src/sandbox/pool.py`
2. 实现 `ContainerPool` 类:
   - `_workers: Dict[str, WorkerInfo]` - 所有 Worker
   - `_idle_queue: asyncio.Queue` - 空闲 Worker 队列
   - `_session_map: Dict[str, str]` - session_id → container_id

3. 核心方法:
   - `initialize()`: 预启动 `min_size` 个 Worker
   - `acquire(session_id) -> WorkerInfo`: 从队列获取 Worker
   - `release(session_id)`: 重置 Worker，归还队列
   - `_replenish_loop()`: 后台线程，保持最小容器数

4. 配置项:
   | 配置项 | 默认值 | 说明 |
   |--------|--------|------|
   | pool_min_size | 2 | 最小容器数 |
   | pool_max_size | 5 | 最大容器数 |

**验证点**:
- [ ] 启动时预热 `min_size` 个容器
- [ ] `create_session()` 延迟 < 500ms（使用预热容器）
- [ ] 容器用完后自动补充

---

#### 任务 4.4: 资源限制

**目标**: 限制 Worker 的 CPU、内存、进程数

**具体步骤**:
1. 修改容器创建参数:
   ```python
   container = docker_client.containers.run(
       ...,
       mem_limit="2g",
       cpu_quota=100000,  # 1 核
       pids_limit=100,
       read_only=False,   # /tmp 需要写入
       cap_drop=["ALL"],
       security_opt=["no-new-privileges:true"]
   )
   ```

**验证点**:
- [ ] 内存超限的代码被 OOM Kill
- [ ] CPU 密集代码不会影响其他容器

---

#### 任务 4.5: 健康检查

**目标**: 定期检查 Worker 健康状态，替换不健康的容器

**具体步骤**:
1. 在 ContainerPool 中添加健康检查循环:
   ```python
   async def _health_check_loop(self):
       while True:
           await asyncio.sleep(30)
           for worker in list(self._workers.values()):
               if not await self._is_healthy(worker):
                   await self._replace_worker(worker)
   ```
2. 健康检查标准:
   - HTTP `/health` 返回 200
   - 容器状态为 running
   - 内存使用 < 90%

**验证点**:
- [ ] 不健康容器被替换
- [ ] 替换过程不影响其他 Session

---

#### 任务 4.6: 并发锁机制

**目标**: 同一 Session 的请求串行执行，避免竞态条件

**具体步骤**:
1. 为每个 Session 维护一个 `asyncio.Lock`
2. 在 `execute()` 中使用锁:
   ```python
   async def execute(self, session_id, code, result_var):
       async with self._session_locks[session_id]:
           # 执行代码
   ```

**验证点**:
- [ ] 同一 Session 的并发请求按顺序执行
- [ ] 不同 Session 的请求并发执行

---

### 4.4 验证方式

1. **gVisor 验证**:
   ```bash
   # 进入 TableMind 容器
   docker exec -it tablemind bash
   
   # 检查运行时
   docker info | grep -i runtime
   
   # 检查 Worker 使用的运行时
   docker inspect worker-xxx | grep Runtime
   ```

2. **容器池验证**:
   ```bash
   # 启动后观察预热容器
   docker ps  # 应该看到 2 个空闲 Worker
   
   # 快速创建多个 Session
   # 观察容器复用情况
   ```

3. **压力测试**:
   - 并发创建/销毁 10 个 Session
   - 观察容器池行为
   - 观察资源使用

### 4.5 进入阶段 5 的条件

- [ ] gVisor 集成成功（或正确降级）
- [ ] 容器池预热/复用正常
- [ ] 资源限制生效
- [ ] 健康检查正常工作

---

## 阶段 5: 业务集成与离线包

### 5.1 目标

将沙盒系统接入 MCP 工具，并制作离线安装包。

### 5.2 任务清单

#### 任务 5.1: MCP 工具改造

**目标**: 修改 `analyze_data` 等工具，使用沙盒执行代码

**具体步骤**:
1. 在工具中注入 SandboxManager 实例
2. 代码执行改为调用 `sandbox_manager.execute()`
3. 解析 SSE 流，转换为 MCP 响应格式

**改造前**:
```python
# 直接在 MCP 进程执行
df = pd.read_csv(path)
result = df.describe()
```

**改造后**:
```python
# 通过沙盒执行
code = f"df = pd.read_csv('{path}')\nresult = df.describe()"
async for chunk in sandbox_manager.execute(session_id, code, "result"):
    # 解析并返回结果
```

**验证点**:
- [ ] MCP 工具通过沙盒执行代码
- [ ] 状态在多次调用间保持
- [ ] 错误正确返回给客户端

---

#### 任务 5.2: 完善 entrypoint.sh

**目标**: 处理离线镜像加载和初始化逻辑

**完善内容**:
```bash
#!/bin/bash
set -e

# 1. 启动内部 Docker Daemon
dockerd > /var/log/dockerd.log 2>&1 &

# 2. 等待 Docker 就绪
echo "Waiting for internal Docker Daemon..."
RETRY=0
until docker info > /dev/null 2>&1; do
    sleep 1
    RETRY=$((RETRY + 1))
    if [ $RETRY -gt 60 ]; then
        echo "Docker Daemon failed to start"
        exit 1
    fi
done
echo "Internal Docker Daemon is ready"

# 3. 创建内部网络
docker network create tablemind-internal 2>/dev/null || true

# 4. 加载 Worker 镜像（离线场景）
if [ -f "/offline_images/worker.tar" ]; then
    # 检查镜像是否已存在
    if ! docker images | grep -q "tablemind/worker"; then
        echo "Loading Worker image..."
        docker load -i /offline_images/worker.tar
    else
        echo "Worker image already loaded"
    fi
fi

# 5. 检查 gVisor 可用性
if docker info 2>/dev/null | grep -q "runsc"; then
    echo "✅ gVisor runtime is available"
else
    echo "⚠️ gVisor runtime not detected, using default runtime"
fi

# 6. 启动主程序
exec python /app/src/pandas_mcp_server.py
```

---

#### 任务 5.3: 离线打包脚本

**目标**: 一键打包离线安装包

**创建 `scripts/build_offline_package.sh`**:
```bash
#!/bin/bash
set -e

VERSION=${1:-"1.0.0"}
OUTPUT_DIR="dist"

echo "Building TableMind offline package v${VERSION}..."

# 1. 构建镜像
echo "Building Docker images..."
docker build -t tablemind:${VERSION} .
docker build -t tablemind/worker:latest ./worker/

# 2. 导出镜像
echo "Exporting images..."
mkdir -p ${OUTPUT_DIR}/offline_images
docker save tablemind:${VERSION} -o ${OUTPUT_DIR}/tablemind.tar
docker save tablemind/worker:latest -o ${OUTPUT_DIR}/offline_images/worker.tar

# 3. 复制配置文件
echo "Copying configuration files..."
cp deploy/docker-compose.yml ${OUTPUT_DIR}/
cp scripts/install.sh ${OUTPUT_DIR}/

# 4. 创建 README
cat > ${OUTPUT_DIR}/README.md << 'EOF'
# TableMind 离线安装包

## 安装步骤

1. 解压安装包
2. 修改 docker-compose.yml 中的数据路径
3. 运行 install.sh

## 配置说明

修改 docker-compose.yml 中的 volumes:
- /your/data/path:/data:ro  → 改为你的数据目录

## 启动服务

```bash
docker-compose up -d
```

## 验证安装

```bash
curl http://localhost:17414/health
```
EOF

# 5. 创建安装脚本
cat > ${OUTPUT_DIR}/install.sh << 'EOF'
#!/bin/bash
set -e

echo "Loading TableMind image..."
docker load < tablemind.tar

echo "Starting TableMind..."
docker-compose up -d

echo "Installation complete!"
echo "Access TableMind at http://localhost:17414"
EOF
chmod +x ${OUTPUT_DIR}/install.sh

# 6. 打包
echo "Creating archive..."
cd ${OUTPUT_DIR}
tar czf ../tablemind-offline-v${VERSION}.tar.gz .
cd ..

echo "Done! Package created: tablemind-offline-v${VERSION}.tar.gz"
```

---

#### 任务 5.4: 编写部署文档

**目标**: 编写客户可用的部署文档

**文档内容**:
1. 系统要求
   - Docker 版本 >= 20.10
   - 可用内存 >= 4GB
   - 磁盘空间 >= 10GB

2. 安装步骤
   - 解压安装包
   - 修改配置（数据路径）
   - 运行安装脚本

3. 验证安装
   - 健康检查
   - 功能测试

4. 常见问题
   - gVisor 兼容性问题
   - 权限问题
   - 网络问题

---

### 5.3 验证方式

1. **端到端测试**:
   - 通过 MCP 客户端调用 analyze_data
   - 验证代码执行、状态保持、结果返回

2. **离线安装测试**:
   - 在干净的机器上测试离线安装包
   - 验证不联网情况下能正常工作

3. **验证清单**:
   - [ ] MCP 工具正常工作
   - [ ] 离线安装包可用
   - [ ] 部署文档完整

---

## 附录 A: 项目完整结构

```
TableMind/
├── src/
│   ├── pandas_mcp_server.py          # FastMCP 入口
│   ├── config.py                     # 配置
│   │
│   ├── sandbox/                      # 沙盒模块
│   │   ├── __init__.py
│   │   ├── manager.py                # SandboxManager 主类
│   │   ├── session.py                # Session 管理
│   │   ├── pool.py                   # 容器池管理
│   │   ├── docker_client.py          # Docker 操作封装
│   │   └── models.py                 # 数据模型
│   │
│   └── tools/                        # MCP 工具
│       ├── analyze_data.py           # 数据分析工具
│       └── ...
│
├── worker/                           # Worker 服务
│   ├── main.py                       # FastAPI 入口
│   ├── requirements.txt
│   ├── Dockerfile
│   └── core/
│       ├── executor.py               # IPython 执行引擎
│       ├── output_capture.py         # 输出捕获
│       └── serializer.py             # 变量序列化
│
├── deploy/
│   ├── docker-compose.yml            # 编排配置
│   ├── daemon.json                   # 内部 Docker 配置
│   └── entrypoint.sh                 # 启动脚本
│
├── scripts/
│   ├── build_offline_package.sh      # 打包脚本
│   └── install.sh                    # 安装脚本
│
├── docs/
│   ├── SANDBOX_FINAL_DESIGN.md       # 最终设计文档
│   ├── SANDBOX_DEVELOPMENT_PLAN.md   # 开发计划（本文档）
│   └── DEPLOYMENT_GUIDE.md           # 部署指南
│
├── Dockerfile                        # TableMind 镜像 (DinD)
└── requirements.txt
```

---

## 附录 B: 阶段依赖关系

```
阶段 1 (Worker 核心)
    └── 阶段 2 (SandboxManager)
            └── 阶段 3 (DinD 架构)
                    └── 阶段 4 (安全与增强)
                            └── 阶段 5 (业务集成)
```

**关键依赖**:
- 阶段 2 依赖阶段 1 的 Worker 镜像
- 阶段 3 复用阶段 2 的 SandboxManager 代码
- 阶段 4 是独立的增强，不影响核心逻辑
- 阶段 5 依赖前面所有阶段

---

## 附录 C: 风险与应对

| 风险 | 影响 | 应对措施 |
|------|------|----------|
| gVisor 兼容性问题 | 某些 Python 库不兼容 | 自动降级为 runc |
| DinD 性能开销 | 约 5-10% 额外开销 | 可接受，容器池缓解冷启动 |
| 级联挂载路径错误 | Worker 无法读取文件 | 阶段 3 重点调试 |
| IPython 输出捕获不完整 | 部分输出丢失 | 阶段 1 重点测试 |

---

## 附录 D: 检查清单

### 阶段 1 完成检查
- [x] IPython 状态保持正常
- [x] SSE 流式输出正常
- [x] 变量序列化正常
- [x] 脏变量清理正常
- [x] Worker 镜像构建成功

### 阶段 2 完成检查
- [ ] 容器创建/销毁正常
- [ ] Session 管理正常
- [ ] HTTP 转发正常
- [ ] 文件挂载正常

### 阶段 3 完成检查
- [ ] DinD 架构运行正常
- [ ] 级联挂载正常
- [ ] 内部网络连通正常

### 阶段 4 完成检查
- [ ] gVisor 集成成功（或正确降级）
- [ ] 容器池工作正常
- [ ] 资源限制生效
- [ ] 健康检查正常

### 阶段 5 完成检查
- [ ] MCP 工具改造完成
- [ ] 离线安装包可用
- [ ] 部署文档完整


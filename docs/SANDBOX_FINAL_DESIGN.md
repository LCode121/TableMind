# TableMind 代码执行沙盒 - 最终设计方案

## 一、概述

### 1.1 核心需求

| 需求 | 方案 |
|------|------|
| 代码执行隔离 | DinD + gVisor |
| 执行引擎 | IPython InteractiveShell |
| 状态保持 | Session 机制（代码生成一段执行一段） |
| 高并发 | 容器池预热 + 动态扩缩容 |
| 文件读写 | Docker 卷挂载（用户指定路径） |
| 流式输出 | SSE (Server-Sent Events) |
| 离线部署 | 镜像打包（gVisor 内置于镜像） |

### 1.2 技术选型

| 技术 | 选型 | 说明 |
|------|------|------|
| 隔离架构 | DinD (Docker in Docker) | TableMind 容器内运行独立 Docker Daemon |
| 安全隔离 | gVisor (runsc) | 打包在镜像内，无需宿主机安装 |
| 执行引擎 | IPython InteractiveShell | 支持状态保持、丰富输出 |
| 通信协议 | HTTP + SSE | 流式推送执行结果 |
| 容器管理 | Docker SDK for Python | 管理 Worker 生命周期 |

### 1.3 为什么选择 DinD 而非 DooD

| 对比项 | DooD | DinD (采用) |
|--------|------|-------------|
| 部署复杂度 | 需要修改宿主机 Docker 配置 | 一个 docker-compose up 搞定 |
| sudo 权限 | 需要 root 安装 gVisor | 不需要，仅需运行 docker-compose |
| 客户接受度 | 低（需修改生产环境配置） | 高（对客户环境零侵入） |
| 离线打包 | 需要单独打包 runsc 二进制 | 一切都在镜像内，docker save 即可 |
| gVisor 安装 | 安装到宿主机 /usr/bin/ | 打包在镜像内，自动配置 |
| 隔离性 | Worker 与宿主机其他容器同级 | Worker 完全在 TableMind 内部 |
| privileged | 不需要 | 需要（但用户代码仍被 gVisor 隔离） |
| 性能 | 更好 | 约 5-10% 开销（可接受） |

**选择 DinD 的核心原因：**
- 客户环境无需任何修改，不需要 sudo 权限
- 完全自包含，离线部署简单
- gVisor 打包在镜像内，无需手动安装配置

---

## 二、系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                   TableMind 沙盒系统架构（DinD 方案）                         │
└─────────────────────────────────────────────────────────────────────────────┘

                    客户环境（宿主机无需任何修改）
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  TableMind 容器 (privileged: true)                                          │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  内置 Docker Daemon                                                  │   │
│  │  • runsc (gVisor) 二进制打包在镜像内                                  │   │
│  │  • daemon.json 预配置 gVisor runtime                                 │   │
│  │  • 存储驱动: vfs 或 overlay2                                          │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  FastMCP Server (:17414)                                             │   │
│  │  ├── analyze_data 工具                                               │   │
│  │  ├── table_operation 工具                                            │   │
│  │  └── get_preview_data 工具                                           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  SandboxManager (内置模块)                                           │   │
│  │  ├── 通过内部 docker.sock 管理 Worker                                │   │
│  │  ├── 容器池管理 (min: 2, max: 5)                                     │   │
│  │  ├── Session → Worker 映射                                           │   │
│  │  └── 代码执行调度                                                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              │                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Worker 容器池 (内部 Docker 创建，runtime=runsc)                     │   │
│  │                                                                      │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                  │   │
│  │  │  Worker-1   │  │  Worker-2   │  │  Worker-N   │                  │   │
│  │  │  Session:A  │  │  Session:B  │  │  (空闲)     │                  │   │
│  │  │             │  │             │  │             │                  │   │
│  │  │ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │                  │   │
│  │  │ │ Sentry  │ │  │ │ Sentry  │ │  │ │ Sentry  │ │  ← gVisor 隔离  │   │
│  │  │ │ 用户态  │ │  │ │ 用户态  │ │  │ │ 用户态  │ │                  │   │
│  │  │ │ 内核    │ │  │ │ 内核    │ │  │ │ 内核    │ │                  │   │
│  │  │ └─────────┘ │  │ └─────────┘ │  │ └─────────┘ │                  │   │
│  │  │             │  │             │  │             │                  │   │
│  │  │ ┌─────────┐ │  │ ┌─────────┐ │  │ ┌─────────┐ │                  │   │
│  │  │ │ IPython │ │  │ │ IPython │ │  │ │ IPython │ │  ← 状态保持     │   │
│  │  │ │ Shell   │ │  │ │ Shell   │ │  │ │ Shell   │ │                  │   │
│  │  │ └─────────┘ │  │ └─────────┘ │  │ └─────────┘ │                  │   │
│  │  │             │  │             │  │             │                  │   │
│  │  │ FastAPI     │  │ FastAPI     │  │ FastAPI     │                  │   │
│  │  │ :9000       │  │ :9000       │  │ :9000       │                  │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  挂载卷：                                                                    │
│  └── 用户指定路径 → /data (Worker 可读取)                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

关键设计点：
• TableMind 容器内运行独立 Docker Daemon（DinD）
• gVisor 打包在镜像内，无需宿主机安装
• 用户指定的数据路径挂载到容器内，Worker 可直接读取
• Worker 由内部 Docker 创建，使用 gVisor 隔离
• 客户环境零侵入，无需 sudo 权限
```

### 2.2 组件职责

| 组件 | 职责 | 运行位置 |
|------|------|----------|
| FastMCP Server | MCP 协议服务，暴露工具接口 | TableMind 容器 |
| 内部 Docker Daemon | 管理 Worker 容器生命周期 | TableMind 容器 |
| SandboxManager | 容器池管理、Session 管理、代码执行调度 | TableMind 容器（内置模块） |
| ReActExecutor | ReAct 循环编排、对话管理、步骤通知 | TableMind 容器（内置模块） |
| Worker | IPython 代码执行、状态保持、流式输出 | Worker 容器（内部 Docker 创建） |

### 2.3 安全边界

| 层级 | 安全机制 | 说明 |
|------|----------|------|
| 第一层 | Docker 容器 | namespace 隔离、cgroup 资源限制、seccomp 过滤 |
| 第二层 | gVisor Sentry | 用户态内核，syscall 拦截，最小化与宿主机交互 |
| 第三层 | 网络隔离 | 内部网络 (internal: true)，Worker 无法访问外网 |
| 第四层 | 资源限制 | CPU、内存、进程数、文件大小限制 |

**privileged 权限说明：**

TableMind 容器需要 privileged 权限以运行内部 Docker Daemon，但这不影响安全性：
- privileged 权限仅用于运行内部 Docker Daemon
- 用户代码在 Worker 容器中执行，受 gVisor 隔离
- 用户恶意代码无法直接利用 TableMind 容器的 privileged 权限

---

## 三、执行引擎

### 3.1 IPython InteractiveShell

Worker 使用 IPython InteractiveShell 作为执行引擎，支持：
- **状态保持**：变量、函数定义在 Session 内持久化
- **丰富输出**：支持 text、image、error 多种输出类型
- **魔法命令**：可扩展支持 %matplotlib 等

### 3.2 状态保持机制

每个 Session 绑定一个独立的 Worker 容器，Worker 内的 IPython Shell 实例在整个 Session 生命周期内保持状态：

| 状态类型 | 说明 | 示例 |
|----------|------|------|
| user_ns | 用户命名空间，存储变量 | df, result, cleaned_df |
| user_global_ns | 全局命名空间，存储导入的模块 | pandas, numpy |
| history | 执行历史 | In[1], In[2], ... |

**代码生成一段执行一段的流程：**

1. 第1段代码：`import pandas as pd` → 执行 → pd 保存到 user_global_ns
2. 第2段代码：`df = pd.read_csv('/data/file.csv')` → 执行 → df 保存到 user_ns
3. 第3段代码：`result = df.describe()` → 执行 → 使用已有的 df，result 保存到 user_ns

所有代码在同一个 IPython Shell 中执行，变量在 Session 内持续有效。

### 3.3 Session 与 Worker 绑定

| 操作 | 说明 |
|------|------|
| 创建 Session | 从空闲池获取 Worker，建立绑定关系 |
| 执行代码 | 根据 session_id 找到绑定的 Worker，转发请求 |
| 释放 Session | 重置 Worker 的 IPython 状态，归还到空闲池 |
| 超时销毁 | 空闲超时或达到最大生存时间，销毁 Worker |

---

## 四、API 设计

### 4.1 SandboxManager 内部 API

| 方法 | 说明 | 返回 |
|------|------|------|
| `create_session()` | 创建 Session，分配 Worker | session_id |
| `execute(session_id, code, result_var)` | 执行代码，流式返回结果 | AsyncIterator[chunk] |
| `release_session(session_id)` | 释放 Session，归还 Worker | void |
| `initialize()` | 启动时初始化（清理 + 预热） | void |

### 4.2 Worker API (容器内部)

| 端点 | 方法 | 说明 |
|------|------|------|
| `/exec` | POST | 执行代码，SSE 流式返回 |
| `/reset` | POST | 重置 IPython 状态 |
| `/health` | GET | 健康检查 |

### 4.3 执行请求格式

**请求：**
```json
{
    "code": "import pandas as pd\ndf = pd.read_csv('/data/file.csv')",
    "result_var": "df"
}
```

**响应（SSE 流）：**
```
data: <txt>文本输出</txt>
data: <err>错误信息</err>
data: <img>base64编码图片</img>
data: <result>{"success": true, "execution_time": 0.234, "return_value": {...}}</result>
```

### 4.4 变量序列化

当指定 `result_var` 时，Worker 会提取并序列化该变量：

| 类型 | 序列化内容 |
|------|------------|
| DataFrame | type, shape, columns, dtypes, preview (前10行), markdown |
| Series | type, name, dtype, data (前100项) |
| dict | type, data |
| list | type, length, data (前100项) |
| 其他 | type, repr (前1000字符) |

---

## 五、文件系统设计

### 5.1 挂载方式

用户指定一个宿主机路径，该路径会被挂载到 TableMind 容器和 Worker 容器中：

| 挂载点 | 容器内路径 | 权限 | 说明 |
|--------|------------|------|------|
| 用户指定路径 | /data | 只读 | 用户上传的数据文件 |

**注意：** 
- 用户负责将数据文件放到指定路径
- TableMind 和 Worker 只需要读取该路径
- 无需创建 sessions 目录，所有分析结果通过 SSE 返回

### 5.2 路径传递

代码中使用容器内路径 `/data/` 访问文件：

```python
# 读取数据文件
df = pd.read_csv('/data/uploads/sales_2024.csv')

# 如果需要保存临时结果，使用 /tmp
df.to_csv('/tmp/result.csv', index=False)
```

### 5.3 数据传递原则

| 做法 | 说明 |
|------|------|
| ❌ 错误 | 在 MCP 读取 DataFrame 传给 Worker |
| ✅ 正确 | 传路径给 Worker，让 Worker 自己读取 |

```python
# 错误：跨进程传递 DataFrame 对象
df = pd.read_csv(path)
sandbox.execute(df)  # 不行！

# 正确：传路径，Worker 自己读取
sandbox.execute(f"df = pd.read_csv('{path}')")
```

---

## 六、Session 生命周期

### 6.1 状态流转

| 状态 | 说明 |
|------|------|
| CREATING | 分配 Worker、初始化环境 |
| READY | 等待代码执行请求 |
| EXECUTING | 代码执行中，流式输出 |
| DESTROYING | 停止 Worker、清理资源 |
| DESTROYED | 已销毁 |

### 6.2 超时配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| session_idle_timeout | 1800s | 空闲超时 30 分钟 |
| session_max_lifetime | 7200s | 最大生存时间 2 小时 |
| execution_timeout | 300s | 单次执行超时 5 分钟 |

### 6.3 回收策略

- 定时任务每 60 秒检查空闲超时和最大生存时间
- 超时的 Session 会被自动销毁
- Worker 重置后归还到空闲池复用

---

## 七、容器池管理

### 7.1 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| pool_min_size | 2 | 最小容器数（预热） |
| pool_max_size | 5 | 最大容器数 |
| scale_up_threshold | 0.8 | 使用率超过 80% 扩容 |
| scale_down_threshold | 0.3 | 使用率低于 30% 缩容 |
| health_check_interval | 30s | 健康检查间隔 |

### 7.2 Worker 资源限制

| 资源 | 限制 | 说明 |
|------|------|------|
| 内存 | 2GB | mem_limit |
| CPU | 1 核 | cpu_quota |
| 进程数 | 100 | pids_limit |
| 根文件系统 | 只读 | read_only: true |
| capabilities | 全部移除 | cap_drop: ALL |
| 提权 | 禁止 | no-new-privileges: true |

### 7.3 健康检查

- HTTP 健康端点：`GET /health`
- 容器状态检查：`container.status == "running"`
- 内存使用检查：`memory_usage < memory_limit * 0.9`

不健康的 Worker 会被移除，并补充新的 Worker 到池中。

---

## 八、部署配置

### 8.1 Docker Compose 配置

```yaml
name: tablemind

services:
  tablemind:
    image: tablemind:1.0
    container_name: tablemind
    restart: always
    
    # DinD 需要 privileged 权限
    privileged: true
    
    environment:
      - MCP_TRANSPORT_MODE=streamable-http
      - SERVER_HOST=0.0.0.0
      - SERVER_PORT=17414
      
      # 沙盒配置
      - SANDBOX_ENABLED=true
      - SANDBOX_POOL_MIN_SIZE=2
      - SANDBOX_POOL_MAX_SIZE=5
      - SANDBOX_WORKER_RUNTIME=runsc
      - SANDBOX_WORKER_MEMORY_LIMIT=2g
      - SANDBOX_WORKER_CPU_LIMIT=1.0
      
      # Session 配置
      - SANDBOX_SESSION_IDLE_TIMEOUT=1800
      - SANDBOX_SESSION_MAX_LIFETIME=7200
      - SANDBOX_EXECUTION_TIMEOUT=300
      
    volumes:
      # 用户指定的数据目录（只读）
      - /your/data/path:/data:ro
      
      # 内部 Docker 数据存储
      - tablemind-docker:/var/lib/docker
      
    ports:
      - "17414:17414"
    
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:17414/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  tablemind-docker:
```

### 8.2 内部 Docker Daemon 配置

镜像内预置 `/etc/docker/daemon.json`：

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

### 8.3 环境变量参考

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| SANDBOX_ENABLED | true | 是否启用沙盒 |
| SANDBOX_POOL_MIN_SIZE | 2 | 最小容器数 |
| SANDBOX_POOL_MAX_SIZE | 5 | 最大容器数 |
| SANDBOX_WORKER_IMAGE | tablemind/worker:latest | Worker 镜像 |
| SANDBOX_WORKER_RUNTIME | runsc | 容器运行时（gVisor） |
| SANDBOX_WORKER_MEMORY_LIMIT | 2g | Worker 内存限制 |
| SANDBOX_WORKER_CPU_LIMIT | 1.0 | Worker CPU 限制 |
| SANDBOX_SESSION_IDLE_TIMEOUT | 1800 | 空闲超时（秒） |
| SANDBOX_SESSION_MAX_LIFETIME | 7200 | 最大生存时间（秒） |
| SANDBOX_EXECUTION_TIMEOUT | 300 | 执行超时（秒） |

---

## 九、离线部署

### 9.1 打包内容

离线部署包包含：

| 文件 | 说明 |
|------|------|
| tablemind.tar | TableMind 镜像（包含 gVisor） |
| tablemind-worker.tar | Worker 镜像 |
| docker-compose.yml | 部署配置 |
| install.sh | 安装脚本 |
| README.md | 部署说明 |

### 9.2 打包步骤（有网环境）

```bash
# 1. 构建镜像
docker build -t tablemind:1.0 .
docker build -t tablemind/worker:latest ./worker/

# 2. 导出镜像
docker save tablemind:1.0 -o tablemind.tar
docker save tablemind/worker:latest -o tablemind-worker.tar

# 3. 打包
tar czf tablemind-offline-v1.0.tar.gz \
    tablemind.tar \
    tablemind-worker.tar \
    docker-compose.yml \
    install.sh \
    README.md
```

### 9.3 安装步骤（客户环境）

```bash
# 1. 解压
tar xzf tablemind-offline-v1.0.tar.gz

# 2. 加载镜像（无需 sudo）
docker load < tablemind.tar
docker load < tablemind-worker.tar

# 3. 修改 docker-compose.yml 中的数据路径
# volumes:
#   - /your/data/path:/data:ro

# 4. 启动服务（无需 sudo）
docker-compose up -d
```

**客户无需：**
- sudo 权限
- 修改宿主机 Docker 配置
- 安装 gVisor
- 重启宿主机 Docker daemon

---

## 十、注意事项

### 10.1 状态保持边界情况

| 问题 | 解决方案 |
|------|----------|
| 大对象内存占用 | Worker 内存限制 (2GB)、Session 最大生存时间、/reset 接口 |
| 死循环/长时间执行 | 执行超时 (5分钟)、超时后发送 SIGINT |
| 修改全局状态 | 每个 Session 独立 Worker、Session 结束时销毁 |
| 文件句柄泄漏 | 容器级别隔离、Session 超时销毁时自动清理 |

### 10.2 并发安全

| 场景 | 处理方式 |
|------|----------|
| 同一 Session 并发请求 | asyncio.Lock 串行化，保证执行顺序 |
| Session 创建/销毁与执行并发 | 销毁操作也需获取 Session 锁 |
| 容器池耗尽 | 返回 503，客户端实现重试逻辑 |

### 10.3 错误处理

| 错误类型 | 处理方式 |
|----------|----------|
| 代码执行错误 | 通过 `<err>` chunk 返回，Session 状态保持 |
| 执行超时 | 中断执行，返回 TimeoutError，Session 状态保持 |
| Worker 崩溃 | 标记 Session 为 error，客户端需创建新 Session |
| TableMind 重启 | 所有 Session 丢失，启动时清理孤儿容器 |

### 10.4 gVisor 兼容性

如果 gVisor 在某些环境不可用，系统会自动降级：

| 情况 | 处理 |
|------|------|
| gVisor 正常 | 使用 runtime=runsc |
| gVisor 不可用 | 降级为 Docker 原生隔离 + 加强 seccomp |

启动日志会显示 gVisor 状态：
- `✅ gVisor runtime is available`
- `⚠️ gVisor runtime not detected, using default runtime`

---

## 十一、项目结构

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
│   │   └── models.py                 # 数据模型
│   │
│   ├── react/                        # ReAct 执行器
│   │   ├── __init__.py
│   │   ├── executor.py               # ReActExecutor 主类
│   │   ├── conversation.py           # 对话管理
│   │   └── code_parser.py            # 代码解析
│   │
│   └── ...
│
├── worker/                           # Worker 服务
│   ├── main.py                       # FastAPI 入口
│   ├── core/
│   │   ├── executor.py               # IPython 执行引擎
│   │   ├── output_capture.py         # 输出捕获
│   │   └── serializer.py             # 变量序列化
│   ├── Dockerfile
│   └── requirements.txt
│
├── deploy/
│   ├── docker-compose.yml
│   ├── daemon.json                   # 内部 Docker 配置
│   └── entrypoint.sh                 # 启动脚本
│
├── Dockerfile                        # TableMind 镜像 (DinD)
└── requirements.txt
```

---

## 十二、开发计划

### Phase 1: 基础设施
- [ ] TableMind DinD 镜像构建（集成 gVisor）
- [ ] Worker 镜像构建（IPython 执行引擎 + FastAPI）
- [ ] SandboxManager 模块实现
- [ ] 容器池管理（预热、分配、归还）

### Phase 2: ReAct 执行器
- [ ] ReActExecutor 模块实现
- [ ] 代码解析器
- [ ] 对话管理器
- [ ] 步骤通知器

### Phase 3: 工具集成
- [ ] analyze_data 改造（使用沙盒执行）
- [ ] table_operation 改造（使用沙盒执行）
- [ ] SSE 流式输出

### Phase 4: 生产就绪
- [ ] gVisor 兼容性测试
- [ ] 健康检查与自动恢复
- [ ] 超时处理与资源限制
- [ ] 离线部署包

---

## 十三、总结

本设计采用 **DinD (Docker in Docker) + gVisor** 方案：

**核心优势：**
- ✅ 客户环境零侵入，无需 sudo 权限
- ✅ gVisor 打包在镜像内，无需手动安装
- ✅ 一个 docker-compose up 完成部署
- ✅ 离线部署简单，docker save/load 即可
- ✅ 状态保持，代码生成一段执行一段
- ✅ 流式输出，实时推送 print/错误/图片
- ✅ 三层安全隔离（Docker + gVisor + 网络）

**部署要求：**
- 客户有 Docker 环境
- 用户指定数据文件存放路径
- 无需其他任何配置

**预期效果：**
- 启动延迟 < 500ms（预热容器）
- 支持 5+ 并发 Session
- 内存隔离 2GB/Worker
- 执行超时保护 5 分钟

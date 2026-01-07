# MCP 步骤展示改造方案

## 一、概述

本文档描述如何改造 TableMind MCP 服务，使其支持在代码生成和执行过程中向后端流式推送执行步骤通知（Notification），以便前端实时展示分析进度。

### 1.1 核心目标

1. **流式代码生成**：将代码生成从同步改为流式，实时获取生成的代码片段
2. **步骤注释捕获**：在生成代码时，通过特定格式的注释标记关键步骤
3. **流式通知推送**：每捕获到一个步骤，立即通过 Notification 机制推送给后端
4. **兼容现有逻辑**：改造不影响现有的工具调用和结果返回逻辑

### 1.2 整体架构

```
┌─────────────┐    流式代码生成     ┌───────────────────┐
│     LLM     │ ─────────────────→ │  StepNotifier    │
└─────────────┘                    │  (步骤捕获器)      │
                                   └───────────────────┘
                                            │
                                            │ 捕获到步骤注释
                                            ▼
                                   ┌───────────────────┐
                                   │  MCP Context      │
                                   │ (send_notification)│
                                   └───────────────────┘
                                            │
                                            ▼
                                   ┌───────────────────┐
                                   │   后端 Server     │
                                   └───────────────────┘
```

---

## 二、提示词改造

### 2.1 步骤注释格式规范

在代码生成提示词中，要求 LLM 使用特定格式的注释来标记关键步骤：

```python
# @STEP: 步骤描述
```

### 2.2 分析代码提示词改造 (`data/prompts/code_gen/python/v2.md`)

新增以下内容到提示词模板：

```markdown
# 步骤注释要求

在代码中的关键步骤处，必须添加特定格式的步骤注释，格式为：
# @STEP: 步骤描述

步骤注释应覆盖以下关键节点：
1. 数据读取/预处理阶段
2. 数据清洗/转换阶段  
3. 核心计算/分析阶段
4. 结果组织阶段

示例：
```python
import pandas as pd

def analyze(df: pd.DataFrame) -> pd.DataFrame:
    # @STEP: 读取并预览数据
    print(df.head())
    
    # @STEP: 数据清洗与预处理
    df = df.dropna()
    
    # @STEP: 执行核心计算
    result = df.groupby('category').agg({'value': 'sum'})
    
    # @STEP: 组织返回结果
    return result.reset_index()
```

注意：
- 每个 @STEP 注释必须独占一行
- 步骤描述应简洁明了，不超过20个字符
- 合理控制步骤数量，通常3-5个步骤为宜
```

### 2.3 表格操作提示词改造 (`data/prompts/code_gen/python/table_operation_v2.md`)

同样新增步骤注释要求，示例：

```python
def operation(dataframes: List[pd.DataFrame], input_paths: List[str], output_path: str) -> Tuple[pd.DataFrame, str]:
    # @STEP: 加载数据表
    df = dataframes[0]
    
    # @STEP: 执行列删除操作
    df = df.drop(columns=['unwanted_col'])
    
    # @STEP: 保存结果文件
    df.to_excel(output_path, index=False)
    
    return df, "删除了unwanted_col列"
```

---

## 三、核心模块设计

### 3.1 步骤通知器 (`StepNotifier`)

新建 `src/step_notifier.py`，负责：
1. 解析流式输出中的步骤注释
2. 调用 MCP Context 发送通知

**核心逻辑**：

```python
class StepNotifier:
    """步骤通知器：解析代码流中的步骤注释并发送通知"""
    
    STEP_PATTERN = r'#\s*@STEP:\s*(.+)'
    
    def __init__(self, context: Context, tool_name: str):
        self.context = context
        self.tool_name = tool_name
        self.buffer = ""
        self.step_count = 0
        
    async def process_chunk(self, chunk: str) -> str:
        """处理流式chunk，捕获步骤注释并发送通知"""
        self.buffer += chunk
        
        # 按行分割，保留未完成的行
        lines = self.buffer.split('\n')
        self.buffer = lines[-1]  # 保留最后未完成的行
        
        for line in lines[:-1]:
            await self._check_and_notify(line)
            
        return chunk
    
    async def _check_and_notify(self, line: str):
        """检查行是否包含步骤注释，如有则发送通知"""
        match = re.search(self.STEP_PATTERN, line)
        if match:
            step_desc = match.group(1).strip()
            self.step_count += 1
            await self._send_notification(step_desc)
    
    async def _send_notification(self, step_desc: str):
        """发送步骤通知给后端"""
        notification_data = {
            "key_step": True,
            "content": "",  # 可选：附加代码片段
            "step": step_desc
        }
        
        await self.context.send_notification(
            method="step_progress",
            params=notification_data
        )
```

### 3.2 通知数据格式

MCP 发送给后端的通知数据格式：

```json
{
  "key_step": true,
  "content": "# @STEP: 读取数据\ndf = pd.read_excel(...)",
  "step": "读取数据"
}
```

后端封装后返回给前端的格式（参考后端方案）：

```json
{
  "retcode": 0,
  "retmsg": "",
  "data": {
    "type": "notice",
    "key_step": true,
    "tool_name": "analyze_data",
    "md_content": "import pandas...",
    "step_list": ["1"]
  }
}
```

---

## 四、代码生成器改造

### 4.1 基类新增流式生成接口

修改 `src/code_generators/python_generator.py`：

```python
class PythonGenerator:
    
    async def generate_code_stream(self, question: str, notifier: StepNotifier = None):
        """
        流式生成代码，同时捕获步骤注释
        
        Args:
            question: 用户问题
            notifier: 步骤通知器（可选）
            
        Yields:
            str: 代码片段
        """
        data_summary = self.data_accessor.get_data_summary()
        prompt = self._build_prompt(question, data_summary)
        
        full_code = ""
        for chunk in self.llm.stream_chat(prompt):
            full_code += chunk
            
            # 如果有通知器，处理步骤捕获
            if notifier:
                await notifier.process_chunk(chunk)
                
            yield chunk
        
        # 处理缓冲区中剩余的内容
        if notifier:
            await notifier.flush()
            
        return full_code
    
    def generate_code(self, question: str):
        """同步版本，保持向后兼容"""
        # 保持原有实现...
```

### 4.2 提示词版本切换

```python
def _load_prompt_tmpl(self, with_steps: bool = False):
    """加载提示词模板"""
    version = "v2" if with_steps else "v1"
    prompt_path = os.path.join(
        config.proj_root, 
        'data', 'prompts', 'code_gen', 'python', 
        f"{version}.md"
    )
    with open(prompt_path, encoding='utf-8') as f:
        return f.read()
```

---

## 五、MCP Server 改造

### 5.1 工具接口改造

修改 `src/pandas_mcp_server.py` 中的 `analyze_data` 工具：

```python
@mcp.tool(
    name='analyze_data',
    description='对数据进行分析，结果以字典数组形式组织'
)
async def analyze_data(
        question: Annotated[str, Field(description="用户问题")],
        path_or_url: Annotated[str, Field(description="数据文件路径或URL")],
        context: Context
) -> Annotated[ToolResult, Field(description="数据分析结果")]:
    
    data_accessor = get_data_accessor(path_or_url)
    
    # 创建步骤通知器
    notifier = StepNotifier(context, tool_name="analyze_data")
    
    code_generator = PythonGenerator(data_accessor, llm)
    code_executor = CodeExecutor(data_accessor, llm)

    try:
        # 使用流式代码生成
        code = ""
        async for chunk in code_generator.generate_code_stream(
            question, 
            notifier=notifier
        ):
            code += chunk
        
        # 代码生成完成通知
        await context.report_progress(
            progress=0.5,
            total=1.0,
            message="代码生成完成",
        )
        
        # 执行代码
        ans_df = code_executor.execute(question, code)
        
        await context.report_progress(
            progress=1.0,
            total=1.0,
            message="分析完成",
        )

    except Exception as e:
        logger.error(traceback.format_exc())
        raise

    return ToolResult(
        content=ans_df.to_markdown(),
        structured_content=ans_df.to_dict(orient='list')
    )
```

### 5.2 Notification 方法说明

FastMCP 的 `Context` 对象提供了发送通知的能力：

```python
# 方式1：使用 send_notification（如果 FastMCP 支持）
await context.send_notification(
    method="step_progress",
    params={"step": "读取数据", "key_step": True}
)

# 方式2：使用 report_progress 的变体
await context.report_progress(
    progress=0.3,
    total=1.0,
    message=json.dumps({"step": "读取数据", "key_step": True})
)

# 方式3：使用自定义日志通知（需后端适配）
await context.send_log(
    level="info",
    data=json.dumps({"step": "读取数据", "key_step": True})
)
```

---

## 六、关键步骤定义

### 6.1 数据分析工具 (`analyze_data`) 关键步骤

| 步骤序号 | 步骤名称 | 触发时机 |
|---------|---------|---------|
| 1 | 读取表格数据 | 开始读取/加载数据 |
| 2 | 数据预处理 | 清洗、类型转换等 |
| 3 | 执行分析计算 | 核心分析逻辑 |
| 4 | 组织结果数据 | 格式化输出结果 |

### 6.2 表格操作工具 (`Table_operation`) 关键步骤

| 步骤序号 | 步骤名称 | 触发时机 |
|---------|---------|---------|
| 1 | 加载数据表 | 读取输入文件 |
| 2 | 执行转换操作 | 核心操作逻辑 |
| 3 | 保存结果文件 | 写入输出文件 |

---

## 七、文件变更清单

### 7.1 新增文件

| 文件路径 | 说明 |
|---------|------|
| `src/step_notifier.py` | 步骤通知器模块 |
| `data/prompts/code_gen/python/v2.md` | 带步骤注释要求的分析提示词 |
| `data/prompts/code_gen/python/table_operation_v2.md` | 带步骤注释要求的操作提示词 |

### 7.2 修改文件

| 文件路径 | 修改内容 |
|---------|---------|
| `src/pandas_mcp_server.py` | 集成 StepNotifier，改用流式生成 |
| `src/code_generators/python_generator.py` | 新增 `generate_code_stream` 方法 |
| `src/code_generators/table_operation_generator.py` | 新增 `generate_code_stream` 方法 |
| `src/llms/base_llm.py` | 确保 `stream_chat` 接口规范 |

---

## 八、时序图

```
┌─────┐     ┌─────────┐     ┌──────────────┐     ┌───────────┐     ┌──────┐
│后端  │     │MCP Server│     │CodeGenerator │     │StepNotifier│     │ LLM  │
└──┬──┘     └────┬────┘     └──────┬───────┘     └─────┬─────┘     └──┬───┘
   │             │                  │                   │              │
   │ tool_call   │                  │                   │              │
   │────────────>│                  │                   │              │
   │             │                  │                   │              │
   │             │ generate_code_stream                 │              │
   │             │─────────────────>│                   │              │
   │             │                  │                   │              │
   │             │                  │   stream_chat     │              │
   │             │                  │──────────────────────────────────>
   │             │                  │                   │              │
   │             │                  │   chunk1          │              │
   │             │                  │<─────────────────────────────────│
   │             │                  │                   │              │
   │             │                  │ process_chunk     │              │
   │             │                  │──────────────────>│              │
   │             │                  │                   │              │
   │             │                  │   检测到 @STEP    │              │
   │             │                  │                   │              │
   │  notification (step1)          │                   │              │
   │<───────────────────────────────────────────────────│              │
   │             │                  │                   │              │
   │             │                  │   chunk2          │              │
   │             │                  │<─────────────────────────────────│
   │             │                  │                   │              │
   │             │                  │ process_chunk     │              │
   │             │                  │──────────────────>│              │
   │             │                  │                   │              │
   │  notification (step2)          │                   │              │
   │<───────────────────────────────────────────────────│              │
   │             │                  │                   │              │
   │             │   ... 更多chunks ...                 │              │
   │             │                  │                   │              │
   │             │ code_complete    │                   │              │
   │             │<─────────────────│                   │              │
   │             │                  │                   │              │
   │             │ execute_code     │                   │              │
   │             │                  │                   │              │
   │ tool_result │                  │                   │              │
   │<────────────│                  │                   │              │
   │             │                  │                   │              │
```

---

## 九、测试要点

### 9.1 单元测试

1. **步骤注释解析测试**
   - 正常格式：`# @STEP: 读取数据`
   - 带空格：`#  @STEP:  读取数据  `
   - 多行内容中的步骤注释

2. **流式生成测试**
   - 验证代码完整性
   - 验证步骤捕获顺序
   - 验证通知发送正确性

### 9.2 集成测试

1. 端到端测试：后端调用 → MCP → 通知推送 → 结果返回
2. 并发请求测试：多个请求同时进行时的隔离性
3. 异常处理测试：LLM 超时、代码执行失败等场景

---

## 十、注意事项

1. **性能考虑**
   - 流式处理增加了复杂度，但提升了用户体验
   - 注释解析使用正则，需保证效率

2. **兼容性**
   - 保留原有同步接口，支持降级
   - 提示词版本可配置切换

3. **错误处理**
   - 如果步骤注释格式不规范，静默跳过，不影响主流程
   - LLM 未生成步骤注释时，仍能正常完成分析

4. **后端配合**
   - 后端需要监听 MCP 的 notification 事件
   - 后端需按约定格式封装后推送给前端

---

## 十一、实施计划

| 阶段 | 内容 | 预估时间 |
|-----|------|---------|
| Phase 1 | 提示词模板改造 + 本地验证 | 0.5 天 |
| Phase 2 | StepNotifier 模块开发 | 0.5 天 |
| Phase 3 | 代码生成器流式改造 | 0.5 天 |
| Phase 4 | MCP Server 集成 | 0.5 天 |
| Phase 5 | 联调测试 | 1 天 |

**总计：约 3 天**

---

## 附录：FastMCP Notification 机制调研

FastMCP 基于 MCP 协议，支持以下通知方式：

```python
# 1. Progress Notification (内置)
await context.report_progress(progress, total, message)

# 2. Log Notification (内置)
await context.send_log(level, data, logger_name)

# 3. 自定义 Notification (需确认版本支持)
await context.send_notification(method, params)
```

如果 `send_notification` 不可用，可通过 `report_progress` 的 `message` 字段传递 JSON 格式的步骤信息，后端解析处理。


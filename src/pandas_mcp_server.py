"""
 Created by Steven Luo on 2025/8/6
"""
import os
import traceback
from typing import List, Dict, Annotated

from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolResult
from pydantic import Field

import config
import utils
from code_executor import CodeExecutor
from code_generators.python_generator import PythonGenerator
from code_generators.table_transform_generator import TableTransformGenerator
from table_transform_executor import TableTransformExecutor
from data_accessors.csv_accessor import CSVAccessor
from data_accessors.excel_accessor import ExcelAccessor
from llms.chat_openai import ChatOpenAI

mcp = FastMCP('data-analyzer', port=8000)

ACCESS_TOKEN = config.get_config()['mcp_server_token']

logger = utils.get_logger(__name__)

llm = ChatOpenAI()

def get_bearer_token(ctx):
    request = ctx.get_http_request()
    headers = request.headers
    # Check if 'Authorization' header is present
    authorization_header = headers.get('Authorization')

    if authorization_header:
        # Split the header into 'Bearer <token>'
        parts = authorization_header.split()

        if len(parts) == 2 and parts[0] == 'Bearer' and parts[1] == ACCESS_TOKEN:
            return parts[1]
        else:
            raise ValueError("Invalid Authorization header format")
    else:
        raise ValueError("Authorization header missing")


def get_data_accessor(path_or_url: str):
    if path_or_url.lower().startswith('http'):
        try:
            data_accessor = CSVAccessor(path_or_url)
        except Exception as e:
            logger.info(e)
            data_accessor = ExcelAccessor(path_or_url)
    elif path_or_url.lower().endswith('csv'):
        data_accessor = CSVAccessor(path_or_url)
    elif path_or_url.lower().endswith('xlsx'):
        data_accessor = ExcelAccessor(path_or_url)
    else:
        raise TypeError("文件类型不支持")
    return data_accessor

@mcp.prompt(
    name='get_prompt',
    title='获取Prompt',
    description='获取数据探查提示',
    tags={"analysis", "data"},
    meta={"version": "1.1", "author": "data-team"}
)
async def get_prompt(
    path_or_url: Annotated[str, Field(description="数据文件路径或URL，仅支持Excel和CSV")],
    context: Context
) -> str:
    return await get_preview_data(path_or_url, context)

@mcp.tool(name='get_preview_data', description='数据描述信息')
async def get_preview_data(
        path_or_url: Annotated[str, Field(description="数据文件路径或URL，仅支持Excel和CSV")],
        context: Context
) -> str:
    """
    以AI易读的格式获取数据信息

    Args:
        path_or_url: 数据文件路径或URL，仅支持Excel和CSV

    Returns:
        以Markdown形式组织的预览结果
    """
    logger.info(f'filepath: {path_or_url}')
    # token = get_bearer_token(context)
    # logger.info(f"Client token: {token}")
    data_accessor = get_data_accessor(path_or_url)
    return "当前数据信息如下：\n" + data_accessor.description


@mcp.tool(
    name='analyze_data',
    description='对数据进行分析，结果以字典数组形式组织'
)
async def analyze_data(
        question: Annotated[str, Field(description="用户问题")],
        path_or_url: Annotated[str, Field(description="数据文件所在路径或者URL，仅支持Excel和CSV")],
        context: Context
) -> Annotated[ToolResult, Field(description="数据分析结果，JSON对象组成的数组")]:
    """
    根据用户问题分析数据

    Args:
        question (str): 用户问题
        path_or_url (str): 数据文件路径，仅支持Excel和CSV

    Returns:
        List[Dict]: 数据分析结果表格，是以字典数组的形式组织的
    """
    # token = get_bearer_token(context)
    # logger.info(f"Client token: {token}")

    logger.info(f'question: {question}')
    logger.info(f'path_or_url: {path_or_url}')

    path_or_url = path_or_url.strip()
    question = question.strip()

    data_accessor = get_data_accessor(path_or_url)
    await context.report_progress(
        progress=0.33,
        total=1.0,
        message="完成数据探查",
    )

    code_generator = PythonGenerator(data_accessor, llm)
    code_executor = CodeExecutor(data_accessor, llm)

    try:
        code = code_generator.generate_code(question)
        await context.report_progress(
            progress=0.67,
            total=1.0,
            message="完成代码生成",
        )

        ans_df = code_executor.execute(question, code)
        await context.report_progress(
            progress=1.0,
            total=1.0,
            message="完成代码执行",
        )

    except Exception as e:
        logger.info(traceback.format_exc())
        raise

    if len(ans_df) > 500:
        logger.info(f'ans_df.shape: {ans_df.shape}, truncate to 500 rows')
        ans_df = ans_df.head(500)

    # structured_content要求是dict类型的
    resp = ans_df.to_dict(orient='list')
    logger.info(f'{question} -> {resp}')

    # 以两种形式返回，方便不同的客户端使用
    return ToolResult(
        content=ans_df.to_markdown(),
        structured_content=resp
    )


def generate_step_output_path(original_path: str, step: int) -> str:
    """
    根据原始文件路径和步骤编号生成输出文件路径
    
    Args:
        original_path: 原始文件路径
        step: 步骤编号
        
    Returns:
        带步骤编号的新文件路径，例如：data.xlsx -> data_step_1.xlsx
    """
    dirname = os.path.dirname(original_path)
    basename = os.path.basename(original_path)
    name, ext = os.path.splitext(basename)
    new_filename = f"{name}_step_{step}{ext}"
    return os.path.join(dirname, new_filename)


@mcp.tool(
    name='Table_operation',
    description='对数据表进行转换操作（如插入列、删除列、pivot、melt、筛选、排序等），并保存到新文件'
)
async def transform_table(
        instruction: Annotated[str, Field(description="操作指令，描述需要对表格进行的转换操作")],
        path_or_url: Annotated[str, Field(description="数据文件所在路径或URL，仅支持Excel和CSV")],
        step: Annotated[int, Field(description="步骤编号，用于生成输出文件名，避免覆盖原文件", ge=1)],
        context: Context
) -> Annotated[ToolResult, Field(description="表格转换结果")]:
    """
    根据用户指令对数据表进行转换操作
    
    支持的操作类型包括：
    - 列操作：插入列、删除列、重命名列、修改数据类型等
    - 行操作：筛选、排序、去重、采样等
    - 数据重塑：pivot、melt、透视表等
    - 分组聚合：groupby、窗口函数等
    - 数据清洗：处理缺失值、异常值、字符串处理等
    
    Args:
        instruction (str): 操作指令
        path_or_url (str): 数据文件路径，仅支持Excel和CSV
        step (int): 步骤编号，>=1

    Returns:
        ToolResult: 包含转换后的数据预览和保存路径
    """
    logger.info(f'instruction: {instruction}')
    logger.info(f'path_or_url: {path_or_url}')
    logger.info(f'step: {step}')

    path_or_url = path_or_url.strip()
    instruction = instruction.strip()

    # 获取数据访问器
    data_accessor = get_data_accessor(path_or_url)
    await context.report_progress(
        progress=0.2,
        total=1.0,
        message="完成数据加载",
    )

    # 生成输出路径
    output_path = generate_step_output_path(path_or_url, step)
    logger.info(f'output_path: {output_path}')

    # 生成转换代码
    code_generator = TableTransformGenerator(data_accessor, llm)
    code_executor = TableTransformExecutor(data_accessor, llm)

    try:
        code = code_generator.generate_code(instruction, output_path)
        await context.report_progress(
            progress=0.5,
            total=1.0,
            message="完成代码生成",
        )

        result_df = code_executor.execute(instruction, code, output_path)
        await context.report_progress(
            progress=0.9,
            total=1.0,
            message="完成代码执行",
        )

    except Exception as e:
        logger.error(traceback.format_exc())
        raise

    # 准备返回结果
    preview_rows = min(20, len(result_df))
    preview_df = result_df.head(preview_rows)
    
    result_info = {
        'output_path': output_path,
        'total_rows': len(result_df),
        'total_columns': len(result_df.columns),
        'columns': result_df.columns.tolist(),
        'preview': preview_df.to_dict(orient='list')
    }

    await context.report_progress(
        progress=1.0,
        total=1.0,
        message="转换完成",
    )

    content_text = f"""## 表格转换完成

### 输出信息
- **保存路径**: `{output_path}`
- **总行数**: {len(result_df)}
- **总列数**: {len(result_df.columns)}
- **列名**: {', '.join(result_df.columns.tolist())}

### 数据预览（前{preview_rows}行）
{preview_df.to_markdown()}
"""

    logger.info(f'{instruction} -> saved to {output_path}')

    return ToolResult(
        content=content_text,
        structured_content=result_info
    )


@mcp.tool(
    name='get_table_steps',
    description='获取已执行的转换步骤信息，查看某个文件的所有步骤版本'
)
async def get_transform_steps(
        path_or_url: Annotated[str, Field(description="原始数据文件路径")],
        context: Context
) -> Annotated[str, Field(description="已存在的步骤文件列表")]:
    """
    查看某个文件的所有步骤版本
    
    Args:
        path_or_url: 原始数据文件路径
        
    Returns:
        已存在的步骤文件列表
    """
    path_or_url = path_or_url.strip()
    dirname = os.path.dirname(path_or_url)
    basename = os.path.basename(path_or_url)
    name, ext = os.path.splitext(basename)

    # 查找所有步骤文件
    step_files = []
    if os.path.isdir(dirname) or dirname == '':
        search_dir = dirname if dirname else '.'
        for filename in os.listdir(search_dir):
            if filename.startswith(f"{name}_step_") and filename.endswith(ext):
                filepath = os.path.join(search_dir, filename)
                step_files.append({
                    'filename': filename,
                    'filepath': filepath,
                    'size': os.path.getsize(filepath),
                    'modified': os.path.getmtime(filepath)
                })
    
    # 按步骤编号排序
    step_files.sort(key=lambda x: x['filename'])

    if not step_files:
        return f"未找到 `{basename}` 的任何步骤版本文件"

    result = f"## {basename} 的步骤版本文件\n\n"
    result += "| 步骤 | 文件名 | 文件大小 |\n"
    result += "|------|--------|----------|\n"
    
    for sf in step_files:
        size_kb = sf['size'] / 1024
        result += f"| - | {sf['filename']} | {size_kb:.1f} KB |\n"

    return result


if __name__ == '__main__':
    # # http://localhost:8000/sse
    # mcp.run(transport='sse')

    # # http://localhost:8000/mcp
    # mcp.run(transport='streamable-http')

    mcp.run(transport='stdio')

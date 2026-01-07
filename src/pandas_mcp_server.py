import os
import json
import traceback
from typing import List, Annotated

from fastmcp import FastMCP, Context
from fastmcp.tools.tool import ToolResult
from pydantic import Field

import config
import utils
from code_executor import CodeExecutor
from code_generators.python_generator import PythonGenerator
from code_generators.table_operation_generator import TableOperationGenerator
from table_operation_executor import TableOperationExecutor
from data_accessors.csv_accessor import CSVAccessor
from data_accessors.excel_accessor import ExcelAccessor
from llms.chat_openai import ChatOpenAI

mcp_transport = os.getenv('MCP_TRANSPORT_MODE', 'streamable-http')
server_host = os.getenv('SERVER_HOST', '0.0.0.0')
server_port = int(os.getenv('SERVER_PORT', '8000'))

mcp = FastMCP('data-analyzer')

logger = utils.get_logger(__name__)

llm = ChatOpenAI()


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

@mcp.tool(
    name='get_preview_data', 
    description='获取数据预览信息，包含数据结构（列名、类型、取值范围）和数据质量概况（缺失率、重复行、异常值检测）'
)
async def get_preview_data(
        path_or_url: Annotated[str, Field(description="数据文件路径或URL，仅支持Excel和CSV")],
        context: Context
) -> str:
    """
    获取数据预览信息，包含：
    - 数据结构：列名、数据类型、典型取值、取值范围
    - 数据质量：质量评级、缺失率、重复行、异常值检测
    - 优化建议：针对数据质量问题的处理建议

    Args:
        path_or_url: 数据文件路径或URL，仅支持Excel和CSV

    Returns:
        以Markdown形式组织的数据预览结果，包含结构信息和质量概况
    """
    logger.info(f'filepath: {path_or_url}')

    data_accessor = get_data_accessor(path_or_url)
    
    # 获取质量摘要用于日志记录
    try:
        quality_summary = data_accessor.get_quality_summary()
        if quality_summary:
            logger.info(f"Data quality: {quality_summary['quality_level']}, score: {quality_summary['quality_score']}")
    except Exception as e:
        logger.warning(f"Failed to get quality summary: {e}")
    
    return "# 当前数据信息\n\n" + data_accessor.description


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


@mcp.tool(
    name='Table_operation',
    description='对数据表进行转换操作（如插入列、删除列、pivot、melt、筛选、排序、合并等），结果保存到指定的输出路径'
)
async def operation_table(
        instruction: Annotated[str, Field(description="操作指令，详细描述需要对表格进行的转换操作，例如：'删除A列'、'按日期排序'、'将表1和表2按ID列合并'")],
        input_paths: Annotated[List[str], Field(description="输入文件路径列表，包含完整路径、文件名和后缀。单表操作传1个路径，多表操作（如合并）传多个路径。仅支持Excel(.xlsx)和CSV(.csv)格式")],
        output_path: Annotated[str, Field(description="输出文件的完整路径，包含文件名和后缀。仅支持Excel(.xlsx)和CSV(.csv)格式")],
        context: Context
) -> Annotated[str, Field(description="JSON格式的结果，包含file_path(保存路径)和path_desc(操作描述)")]:
    """
    根据用户指令对数据表进行转换操作
    
    支持的操作类型包括：
    - 列操作：插入列、删除列、重命名列、修改数据类型等
    - 行操作：筛选、排序、去重、采样等
    - 数据重塑：pivot、melt、透视表等
    - 分组聚合：groupby、窗口函数等
    - 数据清洗：处理缺失值、异常值、字符串处理等
    - 多表操作：合并(merge/join)、拼接(concat)等
    
    Args:
        instruction (str): 操作指令，描述需要执行的转换操作
        input_paths (List[str]): 输入文件路径列表，支持单个或多个文件
        output_path (str): 输出文件的完整路径

    Returns:
        str: JSON格式字符串，包含file_path和path_desc
    """
    logger.info(f'instruction: {instruction}')
    logger.info(f'input_paths: {input_paths}')
    logger.info(f'output_path: {output_path}')

    instruction = instruction.strip()
    output_path = output_path.strip()
    input_paths = [p.strip() for p in input_paths]

    # 获取所有输入文件的数据访问器
    data_accessors = [get_data_accessor(p) for p in input_paths]
    await context.report_progress(
        progress=0.2,
        total=1.0,
        message="完成数据加载",
    )

    # 生成转换代码（使用第一个数据访问器作为主表）
    code_generator = TableOperationGenerator(data_accessors, llm)
    code_executor = TableOperationExecutor(data_accessors, llm)

    try:
        code = code_generator.generate_code(instruction, input_paths, output_path)
        await context.report_progress(
            progress=0.5,
            total=1.0,
            message="完成代码生成",
        )

        result_df, operation_desc = code_executor.execute(instruction, code, input_paths, output_path)
        await context.report_progress(
            progress=0.9,
            total=1.0,
            message="完成代码执行",
        )

    except Exception as e:
        logger.error(traceback.format_exc())
        raise

    await context.report_progress(
        progress=1.0,
        total=1.0,
        message="转换完成",
    )

    # 返回JSON格式结果
    result = {
        "file_path": output_path,
        "path_desc": operation_desc if operation_desc else instruction
    }

    logger.info(f'{instruction} -> {json.dumps(result, ensure_ascii=False)}')

    return json.dumps(result, ensure_ascii=False)


if __name__ == '__main__':

    mcp.run(transport=mcp_transport, host=server_host, port=server_port)

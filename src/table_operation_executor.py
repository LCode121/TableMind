import os
import traceback
from typing import Optional, List, Tuple

import pandas as pd

import config
import utils
from data_accessors.base_data_accessor import BaseDataAccessor
from llms.base_llm import BaseLLM
from schema.execution_error_history import ExecutionErrorHistoryItem


class TableOperationExecutor:
    def __init__(self, data_accessors: List[BaseDataAccessor], llm: Optional[BaseLLM] = None):
        """
        初始化表格操作执行器
        
        Args:
            data_accessors: 数据访问器列表，支持多个输入文件
            llm: 语言模型，用于错误纠正
        """
        self.llm = llm
        self.data_accessors = data_accessors
        self.logger = utils.get_logger(self.__class__.__name__)

    def execute(self, instruction: str, code: str, input_paths: List[str], output_path: str) -> Tuple[pd.DataFrame, str]:
        """
        执行表格转换代码
        
        Args:
            instruction: 用户的操作指令
            code: 生成的Python代码
            input_paths: 输入文件路径列表
            output_path: 输出文件路径
            
        Returns:
            Tuple[pd.DataFrame, str]: 转换后的DataFrame和操作描述
        """
        max_retry_count = config.get_config()['max_retry_execution_count']
        self.logger.info(f"max_retry_execution_count: {max_retry_count}")

        error_corrector = TableOperationErrorCorrector(self.llm)
        error_history_list: List[ExecutionErrorHistoryItem] = []
        result_df = pd.DataFrame([])
        operation_desc = instruction

        while len(error_history_list) <= max_retry_count:
            try:
                result_df, operation_desc = self._execute_code(code, input_paths, output_path)
                break
            except Exception as e:
                self.logger.warning(
                    f"retry_count: {len(error_history_list)}\n"
                    f"code: {code}\n"
                    f"exception:\n{traceback.format_exc()}"
                )

                if len(error_history_list) + 1 > max_retry_count:
                    break

                error_history_list.append(ExecutionErrorHistoryItem(code=code, e=e))
                rewritten_code = error_corrector.correct(
                    self.data_accessors, 
                    instruction, 
                    input_paths,
                    output_path,
                    error_history_list
                )
                self.logger.info(f"rewritten_code:\n{rewritten_code}")
                code = rewritten_code

        return result_df, operation_desc

    def _execute_code(self, code: str, input_paths: List[str], output_path: str) -> Tuple[pd.DataFrame, str]:
        """
        执行转换代码
        
        Args:
            code: Python代码
            input_paths: 输入文件路径列表
            output_path: 输出路径
            
        Returns:
            Tuple[pd.DataFrame, str]: 转换后的DataFrame和操作描述
        """
        # 确保输出目录存在
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # 准备所有输入DataFrame
        dataframes = [accessor.dataframe for accessor in self.data_accessors]

        # 在namespace中执行代码
        namespace = {'pd': pd, 'os': os}
        exec(code, namespace, namespace)
        
        # 调用operation函数，传入DataFrame列表、输入路径列表和输出路径
        result = namespace['operation'](dataframes, input_paths, output_path)

        # 处理返回结果
        if isinstance(result, tuple) and len(result) == 2:
            result_df, operation_desc = result
        else:
            result_df = result
            operation_desc = ""

        if isinstance(result_df, pd.DataFrame):
            return result_df, operation_desc
        elif isinstance(result_df, pd.Series):
            return utils.convert_series_to_dataframe(result_df), operation_desc
        else:
            return result_df, operation_desc


class TableOperationErrorCorrector:
    """表格转换代码错误纠正器"""
    
    def __init__(self, llm: BaseLLM):
        self._llm = llm
        self.logger = utils.get_logger(self.__class__.__name__)

    def _load_err_correction_prompt_tmpl(self):
        """加载表格转换错误纠正Prompt"""
        version = 'v1'
        prompt_path = os.path.join(
            config.proj_root, 
            'data', 'prompts', 'code_error_correction', 'python', 
            f"table_operation_{version}.md"
        )
        with open(prompt_path, encoding='utf-8') as f:
            return f.read()

    def _build_error_history_prompt(self, hist_item: ExecutionErrorHistoryItem):
        """构建错误历史提示"""
        error_info = str(hist_item.e)
        return f"""
以下代码执行时报错：
```python
{hist_item.code}
```

报错信息如下：
```
{error_info}
```
"""

    def correct(
        self, 
        data_accessors: List[BaseDataAccessor], 
        instruction: str,
        input_paths: List[str],
        output_path: str,
        error_history: List[ExecutionErrorHistoryItem]
    ) -> str:
        """
        修正代码错误
        
        Args:
            data_accessors: 数据访问器列表
            instruction: 用户操作指令
            input_paths: 输入文件路径列表
            output_path: 输出路径
            error_history: 错误历史
            
        Returns:
            修正后的代码
        """
        # 构建所有输入数据的描述
        data_info_parts = []
        for i, accessor in enumerate(data_accessors):
            data_summary = accessor.get_data_summary()
            data_info_parts.append(f"### 输入文件 {i+1}: {input_paths[i]}\n{data_summary.description}")
        
        data_info = "\n\n".join(data_info_parts)
        input_paths_str = "\n".join([f"- {p}" for p in input_paths])
        
        prompt_tmpl = self._load_err_correction_prompt_tmpl()

        error_history_part = ''
        for hist in error_history:
            error_history_part += self._build_error_history_prompt(hist)

        prompt = prompt_tmpl.replace(
            '{{data_info}}', data_info
        ).replace(
            '{{instruction}}', instruction
        ).replace(
            '{{input_paths}}', input_paths_str
        ).replace(
            '{{output_path}}', output_path
        ).replace(
            '{{error_history}}', error_history_part
        )
        
        self.logger.info(f"prompt: {prompt}")

        raw_rewritten_code = self._llm.chat_with_retry(prompt)
        rewritten_code = utils.extract_code(raw_rewritten_code, 'python')

        return rewritten_code


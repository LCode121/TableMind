import os
import traceback
from typing import Optional, List

import pandas as pd

import config
import utils
from data_accessors.base_data_accessor import BaseDataAccessor
from llms.base_llm import BaseLLM
from schema.execution_error_history import ExecutionErrorHistoryItem


class TableOperationExecutor:
    def __init__(self, data_accessor: BaseDataAccessor, llm: Optional[BaseLLM] = None):
        self.llm = llm
        self.data_accessor = data_accessor
        self.logger = utils.get_logger(self.__class__.__name__)

    def execute(self, instruction: str, code: str, output_path: str) -> pd.DataFrame:
        """
        执行表格转换代码
        
        Args:
            instruction: 用户的操作指令
            code: 生成的Python代码
            output_path: 输出文件路径
            
        Returns:
            转换后的DataFrame
        """
        max_retry_count = config.get_config()['max_retry_execution_count']
        self.logger.info(f"max_retry_execution_count: {max_retry_count}")

        error_corrector = TableOperationErrorCorrector(self.llm)
        error_history_list: List[ExecutionErrorHistoryItem] = []
        result_df = pd.DataFrame([])

        while len(error_history_list) <= max_retry_count:
            try:
                result_df = self._execute_code(code, output_path)
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
                    self.data_accessor, 
                    instruction, 
                    output_path,
                    error_history_list
                )
                self.logger.info(f"rewritten_code:\n{rewritten_code}")
                code = rewritten_code

        return result_df

    def _execute_code(self, code: str, output_path: str) -> pd.DataFrame:
        """
        执行转换代码
        
        Args:
            code: Python代码
            output_path: 输出路径
            
        Returns:
            转换后的DataFrame
        """
        # 确保输出目录存在
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # 在namespace中执行代码
        namespace = {'pd': pd, 'os': os}
        exec(code, namespace, namespace)
        
        df = self.data_accessor.dataframe
        result = namespace['operation'](df, output_path)

        if isinstance(result, pd.DataFrame):
            return result
        elif isinstance(result, pd.Series):
            return utils.convert_series_to_dataframe(result)
        else:
            return result


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
        data_accessor: BaseDataAccessor, 
        instruction: str,
        output_path: str,
        error_history: List[ExecutionErrorHistoryItem]
    ) -> str:
        """
        修正代码错误
        
        Args:
            data_accessor: 数据访问器
            instruction: 用户操作指令
            output_path: 输出路径
            error_history: 错误历史
            
        Returns:
            修正后的代码
        """
        data_summary = data_accessor.get_data_summary()
        prompt_tmpl = self._load_err_correction_prompt_tmpl()

        error_history_part = ''
        for hist in error_history:
            error_history_part += self._build_error_history_prompt(hist)

        prompt = prompt_tmpl.replace(
            '{{data_info}}', data_summary.description
        ).replace(
            '{{instruction}}', instruction
        ).replace(
            '{{output_path}}', output_path
        ).replace(
            '{{error_history}}', error_history_part
        )
        
        self.logger.info(f"prompt: {prompt}")

        raw_rewritten_code = self._llm.chat_with_retry(prompt)
        rewritten_code = utils.extract_code(raw_rewritten_code, 'python')

        return rewritten_code


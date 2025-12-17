import os
from datetime import datetime

import config
import utils
from data_accessors.dataframe_accessor import DataFrameAccessor
from llms.base_llm import BaseLLM
from schema.data_summary import DataSummary


class TableOperationGenerator:
    def __init__(self, data_accessor: DataFrameAccessor, llm: BaseLLM):
        self.llm = llm
        self.data_accessor = data_accessor
        self.df = data_accessor.dataframe
        self.logger = utils.get_logger(self.__class__.__name__)

    def _load_prompt_tmpl(self):
        version = "v1"
        prompt_path = os.path.join(
            config.proj_root, 'data', 'prompts', 'code_gen', 'python', f"table_operation_{version}.md"
        )
        with open(prompt_path, encoding='utf-8') as f:
            prompt_tmpl = f.read()
        return prompt_tmpl

    def _build_prompt(self, instruction: str, data_summary: DataSummary, output_path: str):
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        prompt = self._load_prompt_tmpl().replace(
            '{{instruction}}', instruction
        ).replace(
            '{{current_time}}', current_time
        ).replace(
            '{{data_info}}', data_summary.description
        ).replace(
            '{{output_path}}', output_path
        )

        return prompt

    def generate_code(self, instruction: str, output_path: str):
        """
        生成表格转换代码
        
        Args:
            instruction: 用户的操作指令（如：插入一列、pivot等）
            output_path: 输出文件路径
            
        Returns:
            生成的Python代码
        """
        data_summary = self.data_accessor.get_data_summary()

        prompt = self._build_prompt(instruction, data_summary, output_path)

        self.logger.info('prompt:\n')
        self.logger.info(prompt)

        resp = self.llm.chat_with_retry(prompt)
        self.logger.info(f'generated code raw_resp:\n{resp}')
        code = utils.extract_code(resp, lang='python')
        self.logger.info(f'generated code:\n{code}')
        return code


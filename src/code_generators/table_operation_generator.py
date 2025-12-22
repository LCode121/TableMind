import os
from datetime import datetime
from typing import List

import config
import utils
from data_accessors.dataframe_accessor import DataFrameAccessor
from llms.base_llm import BaseLLM


class TableOperationGenerator:
    def __init__(self, data_accessors: List[DataFrameAccessor], llm: BaseLLM):
        """
        初始化表格操作代码生成器
        
        Args:
            data_accessors: 数据访问器列表，支持多个输入文件
            llm: 语言模型
        """
        self.llm = llm
        self.data_accessors = data_accessors
        self.logger = utils.get_logger(self.__class__.__name__)

    def _load_prompt_tmpl(self):
        version = "v1"
        prompt_path = os.path.join(
            config.proj_root, 'data', 'prompts', 'code_gen', 'python', f"table_operation_{version}.md"
        )
        with open(prompt_path, encoding='utf-8') as f:
            prompt_tmpl = f.read()
        return prompt_tmpl

    def _build_prompt(self, instruction: str, input_paths: List[str], output_path: str):
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 构建所有输入数据的描述
        data_info_parts = []
        for i, accessor in enumerate(self.data_accessors):
            data_summary = accessor.get_data_summary()
            data_info_parts.append(f"### 输入文件 {i+1}: {input_paths[i]}\n{data_summary.description}")
        
        data_info = "\n\n".join(data_info_parts)
        input_paths_str = "\n".join([f"- {p}" for p in input_paths])

        prompt = self._load_prompt_tmpl().replace(
            '{{instruction}}', instruction
        ).replace(
            '{{current_time}}', current_time
        ).replace(
            '{{data_info}}', data_info
        ).replace(
            '{{input_paths}}', input_paths_str
        ).replace(
            '{{output_path}}', output_path
        )

        return prompt

    def generate_code(self, instruction: str, input_paths: List[str], output_path: str):
        """
        生成表格转换代码
        
        Args:
            instruction: 用户的操作指令（如：插入一列、pivot、合并表格等）
            input_paths: 输入文件路径列表
            output_path: 输出文件路径
            
        Returns:
            生成的Python代码
        """
        prompt = self._build_prompt(instruction, input_paths, output_path)

        self.logger.info('prompt:\n')
        self.logger.info(prompt)

        resp = self.llm.chat_with_retry(prompt)
        self.logger.info(f'generated code raw_resp:\n{resp}')
        code = utils.extract_code(resp, lang='python')
        self.logger.info(f'generated code:\n{code}')
        return code


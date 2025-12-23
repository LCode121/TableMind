from abc import ABC, abstractmethod
from datetime import datetime
from textwrap import dedent

import utils


class BaseDataAccessor(ABC):
    def __init__(self):
        self.logger = utils.get_logger(self.__class__.__name__)

    @abstractmethod
    def load_data(self, n_rows=None):
        pass

    @abstractmethod
    def detect_data(self):
        pass

    @abstractmethod
    def execute(self, code, *args, **kwargs):
        pass

    @abstractmethod
    def get_type(self):
        pass

    @abstractmethod
    def get_data_summary(self):
        pass

    def get_quality_summary(self):
        """
        获取数据质量摘要，子类可以重写此方法
        """
        return None

    def get_quality_description(self) -> str:
        """
        获取数据质量描述（Markdown格式），子类可以重写此方法
        """
        return ""

    @property
    def dataframe(self):
        """
        对于文件类型的数据，通过此属性可以获取全部数据，数据库类型的子类无需实现
        :return:
        """
        raise NotImplementedError()

    @property
    def description(self):
        """
        生成完整的数据描述，包含数据结构和质量概况
        """
        data_summary = self.get_data_summary()
        data_descriptions = []
        
        for col in data_summary.columns:
            values = data_summary.column_values[col][:15]
            # 非字符串类型的，只预览5个值
            value_range_info = ''

            columns_description = data_summary.column_descriptions.get(col, '')
            if columns_description != '':
                columns_description = f"列名含义：{columns_description}\n"

            if data_summary.dtypes[col] != 'string' and col in data_summary.column_min_values:
                values = values[:3]
                value_range_info = f"最小取值：{data_summary.column_min_values[col]}\n最大取值：{data_summary.column_max_values[col]}"

            data_info = dedent(f"""
                ------
                列名：{col}
                典型取值：{values}
                字段类型：{data_summary.dtypes[col]}
                """) + columns_description + value_range_info
            data_descriptions.append(data_info)

        table_description = data_summary.table_description
        if table_description is not None and table_description.strip() != '':
            table_description = f"表格描述：{table_description}\n"

        structure_info = table_description + '\n'.join(data_descriptions).strip()

        quality_description = self.get_quality_description()
        
        if quality_description:
            final_data_info = f"""## 📋 数据结构信息

{structure_info}

{quality_description}
"""
        else:
            final_data_info = structure_info

        return final_data_info

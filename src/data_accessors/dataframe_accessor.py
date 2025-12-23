import threading
from abc import abstractmethod
from functools import wraps
from typing import Optional, Callable, Dict, List, Any

import pandas as pd
import numpy as np

import utils
from data_accessors.base_data_accessor import BaseDataAccessor
from schema.data_summary import DataSummary


class DataFrameAccessor(BaseDataAccessor):
    def __init__(self, df: pd.DataFrame, column_description: Optional[dict] = None):
        super().__init__()
        self._df = df
        self.column_description = column_description
        self._data_summary = None
        self._quality_summary = None  # 缓存质量检查结果

    def get_data_summary(self):
        return self._data_summary

    def get_quality_summary(self) -> Dict[str, Any]:
        """
        获取数据质量摘要
        
        Returns:
            包含质量评级、缺失值、重复行、问题列等信息的字典
        """
        if self._quality_summary is not None:
            return self._quality_summary
        
        df = self._df
        if df is None or len(df) == 0:
            return {
                "quality_level": "⚪ 无数据",
                "total_rows": 0,
                "total_columns": 0,
                "issues": ["数据为空"]
            }
        
        total_rows = len(df)
        total_columns = len(df.columns)
        total_cells = df.size
        
        # 缺失值分析
        missing_counts = df.isnull().sum()
        missing_cells = missing_counts.sum()
        missing_rate = (missing_cells / total_cells * 100) if total_cells > 0 else 0
        
        # 找出缺失率高的列（>5%）
        problem_columns = []
        for col in df.columns:
            col_missing_rate = df[col].isnull().mean() * 100
            if col_missing_rate > 5:
                problem_columns.append({
                    "column": col,
                    "missing_rate": round(col_missing_rate, 2),
                    "missing_count": int(df[col].isnull().sum())
                })
        
        # 重复行检测
        duplicate_rows = df.duplicated().sum()
        duplicate_rate = (duplicate_rows / total_rows * 100) if total_rows > 0 else 0
        
        # 数据类型分析
        dtype_summary = {
            "numeric": len(df.select_dtypes(include=[np.number]).columns),
            "string": len(df.select_dtypes(include=['object']).columns),
            "datetime": len(df.select_dtypes(include=['datetime64']).columns),
            "other": len(df.columns) - len(df.select_dtypes(include=[np.number, 'object', 'datetime64']).columns)
        }
        
        # 异常值检测（仅数值列，使用 IQR 方法）
        outlier_columns = []
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        for col in numeric_cols:
            col_data = df[col].dropna()
            if len(col_data) > 0:
                Q1 = col_data.quantile(0.25)
                Q3 = col_data.quantile(0.75)
                IQR = Q3 - Q1
                lower_bound = Q1 - 1.5 * IQR
                upper_bound = Q3 + 1.5 * IQR
                outliers = ((col_data < lower_bound) | (col_data > upper_bound)).sum()
                outlier_rate = outliers / len(col_data) * 100
                if outlier_rate > 5:  # 异常值超过5%才报告
                    outlier_columns.append({
                        "column": col,
                        "outlier_count": int(outliers),
                        "outlier_rate": round(outlier_rate, 2)
                    })
        
        # 计算质量评分和评级
        quality_score = 100
        issues = []
        recommendations = []
        
        # 缺失值扣分
        if missing_rate > 20:
            quality_score -= 30
            issues.append(f"数据缺失严重，整体缺失率 {missing_rate:.1f}%")
            recommendations.append("建议进行缺失值处理（填充或删除）")
        elif missing_rate > 5:
            quality_score -= 15
            issues.append(f"存在缺失值，整体缺失率 {missing_rate:.1f}%")
            recommendations.append("部分列有缺失值，分析时需注意")
        elif missing_rate > 0:
            quality_score -= 5
        
        # 重复行扣分
        if duplicate_rate > 10:
            quality_score -= 20
            issues.append(f"重复数据较多，{duplicate_rows} 行重复 ({duplicate_rate:.1f}%)")
            recommendations.append("建议去除重复行")
        elif duplicate_rate > 1:
            quality_score -= 10
            issues.append(f"存在 {duplicate_rows} 行重复数据")
        
        # 异常值扣分
        if len(outlier_columns) > 0:
            quality_score -= min(len(outlier_columns) * 5, 15)
            issues.append(f"{len(outlier_columns)} 个数值列存在较多异常值")
            recommendations.append("数值列存在异常值，建议检查数据准确性")
        
        # 确定质量评级
        if quality_score >= 90:
            quality_level = "🟢 优秀"
        elif quality_score >= 75:
            quality_level = "🟡 良好"
        elif quality_score >= 60:
            quality_level = "🟠 一般"
        else:
            quality_level = "🔴 需关注"
        
        self._quality_summary = {
            "quality_level": quality_level,
            "quality_score": max(0, round(quality_score, 1)),
            "total_rows": total_rows,
            "total_columns": total_columns,
            "total_cells": total_cells,
            "missing": {
                "total_missing": int(missing_cells),
                "missing_rate": round(missing_rate, 2),
                "problem_columns": problem_columns
            },
            "duplicates": {
                "duplicate_rows": int(duplicate_rows),
                "duplicate_rate": round(duplicate_rate, 2)
            },
            "dtype_summary": dtype_summary,
            "outliers": {
                "detection_method": "IQR",
                "detection_rule": "值 < Q1-1.5×IQR 或 值 > Q3+1.5×IQR",
                "outlier_columns": outlier_columns
            },
            "issues": issues,
            "recommendations": recommendations
        }
        
        return self._quality_summary

    def get_quality_description(self) -> str:
        """
        生成人类可读的质量描述（Markdown格式）
        """
        quality = self.get_quality_summary()
        
        desc = f"""
## 📊 数据质量概况
- **质量评级**: {quality['quality_level']} (评分: {quality['quality_score']}/100)
- **数据规模**: {quality['total_rows']:,} 行 × {quality['total_columns']} 列
- **缺失率**: {quality['missing']['missing_rate']:.2f}% ({quality['missing']['total_missing']:,}/{quality['total_cells']:,})
- **重复行**: {quality['duplicates']['duplicate_rows']:,} 行 ({quality['duplicates']['duplicate_rate']:.2f}%)
"""
        
        # 数据类型分布
        dtype_summary = quality['dtype_summary']
        desc += f"- **列类型**: 数值型 {dtype_summary['numeric']} 列, 文本型 {dtype_summary['string']} 列"
        if dtype_summary['datetime'] > 0:
            desc += f", 日期型 {dtype_summary['datetime']} 列"
        desc += "\n"
        
        # 问题列
        if quality['missing']['problem_columns']:
            desc += "\n### ⚠️ 需关注的列\n"
            for col_info in quality['missing']['problem_columns'][:5]:  # 最多显示5个
                desc += f"- **{col_info['column']}**: 缺失 {col_info['missing_count']} 个值 ({col_info['missing_rate']}%)\n"
        
        # 异常值列
        if quality['outliers']['outlier_columns']:
            desc += "\n### 📈 存在异常值的列\n"
            desc += "> 检测方法：IQR（四分位距）法，判定标准：值 < Q1-1.5×IQR 或 值 > Q3+1.5×IQR\n\n"
            for col_info in quality['outliers']['outlier_columns'][:3]:  # 最多显示3个
                desc += f"- **{col_info['column']}**: {col_info['outlier_count']} 个异常值 ({col_info['outlier_rate']}%)\n"
        
        # 建议
        if quality['recommendations']:
            desc += "\n### 💡 建议\n"
            for rec in quality['recommendations']:
                desc += f"- {rec}\n"
        
        return desc.strip()

    def detect_data(self) -> DataSummary:
        ds_df = self._df
        self.logger.info(f"start detect data, record count: {len(ds_df)}")

        columns = ds_df.columns.tolist()
        data_preview = ds_df[:5].to_dict(orient='records')
        for row in data_preview:
            for k, v in row.items():
                row[k] = utils.process_df_value(row[k])

        dtypes = {col: str(ds_df[col].dtype) for col in ds_df}
        dtypes = {col: 'string' if dtype == 'object' else dtype for col, dtype in dtypes.items()}
        # 按频率统计
        column_values = {col: [utils.process_df_value(v) for v in ds_df[col].value_counts(dropna=False).index.tolist()[:25]] for col in ds_df.columns}

        # table_describe = 'test table describe'
        table_describe = ''
        # column_describes = {col: f'test value {v}' for col in range(len(ds_df.columns))}
        column_describes = self.column_description if self.column_description else {}
        data_summary = DataSummary(
            columns=columns,
            dtypes=dtypes,
            column_values=column_values,
            table_description=table_describe,
            column_descriptions=column_describes,
            column_min_values={col: str(ds_df[col].dropna().min()) for col in ds_df.columns if dtypes[col] != 'string'},
            column_max_values={col: str(ds_df[col].dropna().max()) for col in ds_df.columns if dtypes[col] != 'string'}
        )
        return data_summary

    def execute(self, code, func_name='analyze'):
        """
        执行代码
        :param code: 代码
        :param func_name: 代码中的入口函数，主要用于获取代码执行结果，与prompt中定义的让LLM完成的代码签名一致
        :return: 代码执行结果，pd.DataFrame类型
        """
        # 在namespace中执行，不指定的话，带import语句的代码，只在exec的局部作用域中，函数调用时，无法使用这些依赖
        namespace = {'pd': pd}
        # namespace['dfs'] = [self._df.copy()]
        exec(code, namespace, namespace)
        df = self._df
        res = namespace[func_name](df)
        # res = namespace[func_name]([df.copy()])

        if isinstance(res, pd.DataFrame):
            ret_df = res
        elif isinstance(res, pd.Series):
            ret_df = utils.convert_series_to_dataframe(res)
        elif isinstance(res, dict):
            if res['type'] == 'dataframe':
                ret_df = res['value']
            else:
                ret_df = pd.DataFrame({'结果': [res['value']]})
        else:
            ret_df = res
        return ret_df


    def get_type(self):
        return 'python'

    @property
    def dataframe(self):
        """
        对于文件类型的数据，通过此属性可以获取全部数据，数据库类型的子类无需实现
        :return:
        """
        return self._df

    @abstractmethod
    def load_data(self, filepath, **kwargs):
        pass

    @classmethod
    def cached_data_loader(cls, loader_func: Callable) -> Callable:
        cached = {}
        lock = threading.Lock()

        @wraps(loader_func)
        def wrapper(self, filepath, *args, **kwargs):
            cache_key = (filepath, self.__class__.__name__) + tuple(args) + tuple([f"{k}={v}" for k, v in kwargs.items()])
            # 检查缓存（第一次无锁检查）
            import os
            # 获取文件当前的修改时间
            current_mtime = None
            if os.path.exists(filepath):
                current_mtime = os.path.getmtime(filepath)
            
            if cache_key in cached:
                cached_mtime, cached_df = cached[cache_key]
                # 只有文件修改时间一致时才使用缓存
                if current_mtime is not None and cached_mtime == current_mtime:
                    self.logger.info(f'{cache_key} cache hit (mtime unchanged)')
                    return cached_df.copy()
                else:
                    self.logger.info(f'{cache_key} cache invalidated (file modified: {cached_mtime} -> {current_mtime})')


            with lock:
                # 双重检查避免竞争条件
                if cache_key in cached:
                    cached_mtime, cached_df = cached[cache_key]
                    if current_mtime is not None and cached_mtime == current_mtime:
                        self.logger.info(f'{cache_key} cache hit in lock')
                        return cached_df.copy()

                self.logger.info(f'{cache_key} cache miss, loading file...')
                df = loader_func(self, filepath, *args, **kwargs)
                
                # 存储修改时间和数据
                if current_mtime is not None:
                    cached[cache_key] = (current_mtime, df)
                
                return df.copy()

        return wrapper

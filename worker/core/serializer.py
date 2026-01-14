"""
变量序列化模块

将 Python 对象序列化为 JSON 格式，用于返回给客户端。
"""

import json
from typing import Any, Optional
from datetime import datetime, date


def serialize_variable(var: Any, name: str = "") -> dict:
    """
    序列化 Python 变量为 JSON 可表示的字典
    
    Args:
        var: 要序列化的变量
        name: 变量名
        
    Returns:
        序列化后的字典
    """
    try:
        # 处理 None
        if var is None:
            return {
                "name": name,
                "type": "NoneType",
                "value": None
            }
        
        type_name = type(var).__name__
        
        # DataFrame
        if _is_dataframe(var):
            return _serialize_dataframe(var, name)
        
        # Series
        if _is_series(var):
            return _serialize_series(var, name)
        
        # 基本类型：数值
        if isinstance(var, (int, float)):
            return {
                "name": name,
                "type": type_name,
                "value": var if not (isinstance(var, float) and (var != var)) else None  # 处理 NaN
            }
        
        # 基本类型：布尔
        if isinstance(var, bool):
            return {
                "name": name,
                "type": "bool",
                "value": var
            }
        
        # 基本类型：字符串
        if isinstance(var, str):
            # 限制字符串长度
            max_len = 10000
            value = var[:max_len] if len(var) > max_len else var
            return {
                "name": name,
                "type": "str",
                "value": value,
                "truncated": len(var) > max_len,
                "original_length": len(var)
            }
        
        # 日期时间
        if isinstance(var, datetime):
            return {
                "name": name,
                "type": "datetime",
                "value": var.isoformat()
            }
        
        if isinstance(var, date):
            return {
                "name": name,
                "type": "date",
                "value": var.isoformat()
            }
        
        # 列表
        if isinstance(var, list):
            return _serialize_list(var, name)
        
        # 元组
        if isinstance(var, tuple):
            return _serialize_tuple(var, name)
        
        # 字典
        if isinstance(var, dict):
            return _serialize_dict(var, name)
        
        # 集合
        if isinstance(var, (set, frozenset)):
            return _serialize_set(var, name)
        
        # numpy 数组
        if _is_numpy_array(var):
            return _serialize_numpy_array(var, name)
        
        # 其他类型：使用 repr
        return _serialize_other(var, name)
        
    except Exception as e:
        return {
            "name": name,
            "type": type(var).__name__,
            "error": f"序列化失败: {str(e)}",
            "repr": _safe_repr(var, 500)
        }


def _is_dataframe(var) -> bool:
    """检查是否是 DataFrame"""
    try:
        import pandas as pd
        return isinstance(var, pd.DataFrame)
    except ImportError:
        return False


def _is_series(var) -> bool:
    """检查是否是 Series"""
    try:
        import pandas as pd
        return isinstance(var, pd.Series)
    except ImportError:
        return False


def _is_numpy_array(var) -> bool:
    """检查是否是 numpy 数组"""
    try:
        import numpy as np
        return isinstance(var, np.ndarray)
    except ImportError:
        return False


def _safe_repr(var: Any, max_len: int = 1000) -> str:
    """安全地获取对象的 repr"""
    try:
        r = repr(var)
        if len(r) > max_len:
            return r[:max_len] + "..."
        return r
    except Exception as e:
        return f"<repr failed: {e}>"


def _serialize_dataframe(df, name: str) -> dict:
    """序列化 DataFrame"""
    import pandas as pd
    
    # 获取预览数据（前10行）
    preview_rows = 10
    preview_df = df.head(preview_rows)
    
    # 转换为可序列化的格式
    try:
        # 处理日期时间列
        preview_data = preview_df.copy()
        for col in preview_data.columns:
            if pd.api.types.is_datetime64_any_dtype(preview_data[col]):
                preview_data[col] = preview_data[col].astype(str)
        
        preview_records = preview_data.to_dict(orient='records')
    except Exception:
        preview_records = []
    
    # 生成 markdown 表格
    try:
        markdown = preview_df.to_markdown(index=False)
    except Exception:
        markdown = None
    
    # 获取列信息
    columns_info = []
    for col in df.columns:
        columns_info.append({
            "name": str(col),
            "dtype": str(df[col].dtype),
            "null_count": int(df[col].isnull().sum()),
            "unique_count": int(df[col].nunique()) if len(df) < 100000 else None
        })
    
    return {
        "name": name,
        "type": "DataFrame",
        "shape": list(df.shape),
        "rows": df.shape[0],
        "columns": df.shape[1],
        "column_names": [str(c) for c in df.columns.tolist()],
        "dtypes": {str(k): str(v) for k, v in df.dtypes.to_dict().items()},
        "columns_info": columns_info,
        "preview": preview_records,
        "preview_rows": min(preview_rows, len(df)),
        "markdown": markdown
    }


def _serialize_series(series, name: str) -> dict:
    """序列化 Series"""
    import pandas as pd
    
    # 限制数据量
    max_items = 100
    data = series.head(max_items).tolist()
    
    # 处理不可序列化的值
    serializable_data = []
    for item in data:
        if pd.isna(item):
            serializable_data.append(None)
        elif isinstance(item, (datetime, date)):
            serializable_data.append(item.isoformat())
        else:
            try:
                json.dumps(item)
                serializable_data.append(item)
            except (TypeError, ValueError):
                serializable_data.append(str(item))
    
    return {
        "name": name,
        "type": "Series",
        "series_name": str(series.name) if series.name is not None else None,
        "dtype": str(series.dtype),
        "length": len(series),
        "data": serializable_data,
        "truncated": len(series) > max_items,
        "null_count": int(series.isnull().sum())
    }


def _serialize_list(lst: list, name: str) -> dict:
    """序列化列表"""
    max_items = 100
    
    # 序列化前 N 个元素
    serializable_data = []
    for item in lst[:max_items]:
        try:
            json.dumps(item)
            serializable_data.append(item)
        except (TypeError, ValueError):
            serializable_data.append(_safe_repr(item, 200))
    
    return {
        "name": name,
        "type": "list",
        "length": len(lst),
        "data": serializable_data,
        "truncated": len(lst) > max_items
    }


def _serialize_tuple(tpl: tuple, name: str) -> dict:
    """序列化元组"""
    max_items = 100
    
    serializable_data = []
    for item in tpl[:max_items]:
        try:
            json.dumps(item)
            serializable_data.append(item)
        except (TypeError, ValueError):
            serializable_data.append(_safe_repr(item, 200))
    
    return {
        "name": name,
        "type": "tuple",
        "length": len(tpl),
        "data": serializable_data,
        "truncated": len(tpl) > max_items
    }


def _serialize_dict(d: dict, name: str) -> dict:
    """序列化字典"""
    max_items = 100
    
    # 序列化键值对
    serializable_data = {}
    count = 0
    for key, value in d.items():
        if count >= max_items:
            break
        
        # 序列化键
        try:
            key_str = str(key)
        except:
            key_str = f"<key_{count}>"
        
        # 序列化值
        try:
            json.dumps(value)
            serializable_data[key_str] = value
        except (TypeError, ValueError):
            serializable_data[key_str] = _safe_repr(value, 200)
        
        count += 1
    
    return {
        "name": name,
        "type": "dict",
        "length": len(d),
        "keys": list(serializable_data.keys()),
        "data": serializable_data,
        "truncated": len(d) > max_items
    }


def _serialize_set(s, name: str) -> dict:
    """序列化集合"""
    max_items = 100
    
    serializable_data = []
    for item in list(s)[:max_items]:
        try:
            json.dumps(item)
            serializable_data.append(item)
        except (TypeError, ValueError):
            serializable_data.append(_safe_repr(item, 200))
    
    return {
        "name": name,
        "type": type(s).__name__,
        "length": len(s),
        "data": serializable_data,
        "truncated": len(s) > max_items
    }


def _serialize_numpy_array(arr, name: str) -> dict:
    """序列化 numpy 数组"""
    import numpy as np
    
    max_items = 100
    
    # 展平并取前 N 个元素
    flat = arr.flatten()[:max_items]
    
    # 转换为 Python 原生类型
    data = []
    for item in flat:
        if np.isnan(item) if np.issubdtype(type(item), np.floating) else False:
            data.append(None)
        else:
            try:
                data.append(item.item())  # 转换为 Python 原生类型
            except:
                data.append(str(item))
    
    return {
        "name": name,
        "type": "ndarray",
        "dtype": str(arr.dtype),
        "shape": list(arr.shape),
        "size": arr.size,
        "data": data,
        "truncated": arr.size > max_items
    }


def _serialize_other(var: Any, name: str) -> dict:
    """序列化其他类型"""
    repr_str = _safe_repr(var, 1000)
    
    return {
        "name": name,
        "type": type(var).__name__,
        "repr": repr_str
    }

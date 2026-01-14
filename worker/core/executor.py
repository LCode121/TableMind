"""
IPython 执行引擎

提供代码执行、状态保持、脏变量清理等功能。
"""

import sys
import time
import json
import asyncio
import traceback
from typing import Any, Optional, AsyncIterator, Set
from dataclasses import dataclass, field
from enum import Enum

from IPython.core.interactiveshell import InteractiveShell
from IPython.core.interactiveshell import ExecutionResult

from .output_capture import OutputCapture, OutputType, OutputChunk
from .serializer import serialize_variable


class ExecutionStatus(Enum):
    """执行状态"""
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class ExecutionResultInfo:
    """执行结果信息"""
    success: bool
    status: ExecutionStatus
    execution_time: float
    error_message: Optional[str] = None
    error_type: Optional[str] = None
    traceback: Optional[str] = None
    return_value: Optional[dict] = None
    
    def to_dict(self) -> dict:
        """转换为字典"""
        result = {
            "success": self.success,
            "status": self.status.value,
            "execution_time": round(self.execution_time, 4)
        }
        
        if self.error_message:
            result["error_message"] = self.error_message
        if self.error_type:
            result["error_type"] = self.error_type
        if self.traceback:
            result["traceback"] = self.traceback
        if self.return_value is not None:
            result["return_value"] = self.return_value
        
        return result
    
    def to_json(self) -> str:
        """转换为 JSON 字符串"""
        return json.dumps(self.to_dict(), ensure_ascii=False)


class IPythonExecutor:
    """
    IPython 代码执行器
    
    特性:
    - 状态保持：变量在多次执行间保持
    - 脏变量清理：执行失败时回滚新增变量
    - 输出捕获：实时捕获 stdout/stderr
    """
    
    # 内置变量，不应被清理
    BUILTIN_VARS = {
        '__name__', '__doc__', '__package__', '__loader__', '__spec__',
        '__builtins__', '__builtin__', '_ih', '_oh', '_dh', 
        'In', 'Out', 'get_ipython', 'exit', 'quit',
        '_', '__', '___', '_i', '_ii', '_iii',
        '_i1', '_i2', '_i3', '_1', '_2', '_3',
        # 预加载的库和函数
        'warnings', 'matplotlib', 'open'
    }
    
    def __init__(self):
        """初始化 IPython 执行器"""
        # 创建独立的 InteractiveShell 实例（不使用单例）
        self.shell = InteractiveShell()
        
        # 配置 shell
        self.shell.colors = 'NoColor'  # 禁用颜色，方便捕获
        
        # 预导入常用库
        self._preload_libraries()
    
    def _preload_libraries(self):
        """预加载常用库"""
        preload_code = """
import warnings
warnings.filterwarnings('ignore')

# 设置 matplotlib 非交互式后端
import matplotlib
matplotlib.use('Agg')
"""
        try:
            self.shell.run_cell(preload_code, silent=True)
        except Exception:
            pass  # 忽略预加载错误
    
    @property
    def user_ns(self) -> dict:
        """获取用户命名空间"""
        return self.shell.user_ns
    
    @property
    def user_global_ns(self) -> dict:
        """获取用户全局命名空间"""
        return self.shell.user_global_ns
    
    def get_variable(self, name: str) -> Any:
        """
        从用户命名空间获取变量
        
        Args:
            name: 变量名
            
        Returns:
            变量值，如果不存在则返回 None
        """
        return self.user_ns.get(name)
    
    def set_variable(self, name: str, value: Any):
        """
        设置用户命名空间中的变量
        
        Args:
            name: 变量名
            value: 变量值
        """
        self.user_ns[name] = value
    
    def has_variable(self, name: str) -> bool:
        """检查变量是否存在"""
        return name in self.user_ns
    
    def list_variables(self) -> Set[str]:
        """
        列出所有用户定义的变量
        
        Returns:
            变量名集合（排除内置变量）
        """
        all_vars = set(self.user_ns.keys())
        return all_vars - self.BUILTIN_VARS
    
    def _get_current_keys(self) -> Set[str]:
        """获取当前命名空间的键集合"""
        return set(self.user_ns.keys())
    
    def _cleanup_dirty_variables(self, keys_before: Set[str]):
        """
        清理脏变量（执行失败时回滚新增的变量）
        
        Args:
            keys_before: 执行前的键集合
        """
        keys_after = self._get_current_keys()
        new_keys = keys_after - keys_before
        
        # 删除新增的变量（排除内置变量）
        for key in new_keys:
            if key not in self.BUILTIN_VARS:
                try:
                    del self.user_ns[key]
                except KeyError:
                    pass
    
    def run_code_sync(
        self, 
        code: str, 
        result_var: Optional[str] = None,
        capture: Optional[OutputCapture] = None
    ) -> ExecutionResultInfo:
        """
        同步执行代码
        
        Args:
            code: 要执行的代码
            result_var: 需要返回的变量名
            capture: 输出捕获器（可选）
            
        Returns:
            执行结果信息
        """
        start_time = time.time()
        keys_before = self._get_current_keys()
        
        error_message = None
        error_type = None
        tb_str = None
        success = False
        
        try:
            # 执行代码
            result: ExecutionResult = self.shell.run_cell(code, silent=False)
            
            # 检查是否有错误
            if result.error_in_exec is not None:
                # 运行时错误
                exc = result.error_in_exec
                error_type = type(exc).__name__
                error_message = str(exc)
                tb_str = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                
                # 清理脏变量
                self._cleanup_dirty_variables(keys_before)
                
            elif result.error_before_exec is not None:
                # 语法错误等
                exc = result.error_before_exec
                error_type = type(exc).__name__
                error_message = str(exc)
                tb_str = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                
            else:
                success = True
                
        except Exception as e:
            # 捕获其他异常
            error_type = type(e).__name__
            error_message = str(e)
            tb_str = traceback.format_exc()
            
            # 清理脏变量
            self._cleanup_dirty_variables(keys_before)
        
        execution_time = time.time() - start_time
        
        # 序列化返回值
        return_value = None
        if success and result_var:
            var = self.get_variable(result_var)
            if var is not None:
                return_value = serialize_variable(var, result_var)
        
        # 构造结果
        return ExecutionResultInfo(
            success=success,
            status=ExecutionStatus.SUCCESS if success else ExecutionStatus.ERROR,
            execution_time=execution_time,
            error_message=error_message,
            error_type=error_type,
            traceback=tb_str,
            return_value=return_value
        )
    
    def run_code_with_capture(
        self,
        code: str,
        result_var: Optional[str] = None
    ) -> tuple[list[OutputChunk], ExecutionResultInfo]:
        """
        执行代码并捕获输出（同步版本）
        
        Args:
            code: 要执行的代码
            result_var: 需要返回的变量名
            
        Returns:
            (输出片段列表, 执行结果)
        """
        capture = OutputCapture()
        
        capture.start()
        try:
            result = self.run_code_sync(code, result_var, capture)
        finally:
            capture.stop()
        
        # 收集所有输出
        chunks = capture.drain_queue()
        
        return chunks, result
    
    async def run_code(
        self,
        code: str,
        result_var: Optional[str] = None
    ) -> AsyncIterator[OutputChunk]:
        """
        异步执行代码，流式返回输出
        
        Args:
            code: 要执行的代码
            result_var: 需要返回的变量名
            
        Yields:
            输出片段（text/error/result）
        """
        # 在线程池中执行代码（避免阻塞事件循环）
        loop = asyncio.get_event_loop()
        chunks, result = await loop.run_in_executor(
            None,
            self.run_code_with_capture,
            code,
            result_var
        )
        
        # 输出捕获的内容
        for chunk in chunks:
            yield chunk
        
        # 输出最终结果
        if result:
            yield OutputChunk(
                type=OutputType.RESULT,
                content=result.to_json()
            )
    
    def reset(self):
        """
        重置执行器状态
        
        清空用户命名空间中的所有变量（保留内置变量）
        """
        # 获取需要删除的键
        keys_to_delete = list(self.list_variables())
        
        # 删除变量
        for key in keys_to_delete:
            try:
                del self.user_ns[key]
            except KeyError:
                pass
        
        # 清空历史
        self.shell.reset(new_session=True)
        
        # 重新预加载库
        self._preload_libraries()
    
    def get_execution_count(self) -> int:
        """获取执行计数"""
        return self.shell.execution_count
    
    def get_history(self, last_n: int = 10) -> list:
        """
        获取最近的执行历史
        
        Args:
            last_n: 返回最近 N 条历史
            
        Returns:
            历史记录列表 [(session, line, input), ...]
        """
        try:
            history = list(self.shell.history_manager.get_tail(last_n))
            return history
        except Exception:
            return []


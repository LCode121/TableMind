"""
输出捕获模块

实时捕获 stdout/stderr 输出，支持流式推送。
"""

import sys
import asyncio
from io import StringIO
from typing import Optional, AsyncIterator
from dataclasses import dataclass
from enum import Enum


class OutputType(Enum):
    """输出类型枚举"""
    TEXT = "txt"      # 标准输出
    ERROR = "err"     # 错误输出
    IMAGE = "img"     # 图片 (base64)
    RESULT = "result" # 执行结果


@dataclass
class OutputChunk:
    """输出片段"""
    type: OutputType
    content: str
    
    def to_sse(self) -> str:
        """转换为 SSE 格式"""
        return f"<{self.type.value}>{self.content}</{self.type.value}>"


class StreamCapture:
    """
    流捕获器，替换 sys.stdout/stderr
    
    将写入的内容实时推送到 asyncio.Queue
    """
    
    def __init__(self, queue: asyncio.Queue, output_type: OutputType, original_stream):
        self.queue = queue
        self.output_type = output_type
        self.original_stream = original_stream
        self._buffer = StringIO()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
    
    def write(self, text: str) -> int:
        """捕获写入的文本"""
        if not text:
            return 0
        
        # 同时写入原始流（用于调试）
        if self.original_stream:
            self.original_stream.write(text)
        
        # 将输出放入队列
        chunk = OutputChunk(type=self.output_type, content=text)
        
        # 尝试异步放入队列
        try:
            if self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                except RuntimeError:
                    self._loop = None
            
            if self._loop and self._loop.is_running():
                # 在异步环境中，使用线程安全的方式放入队列
                self._loop.call_soon_threadsafe(
                    lambda: self.queue.put_nowait(chunk)
                )
            else:
                # 非异步环境，直接放入
                try:
                    self.queue.put_nowait(chunk)
                except:
                    pass
        except Exception:
            # 忽略队列放入错误
            pass
        
        return len(text)
    
    def flush(self):
        """刷新流"""
        if self.original_stream:
            self.original_stream.flush()
    
    def isatty(self) -> bool:
        """是否是终端"""
        return False
    
    @property
    def encoding(self) -> str:
        """编码"""
        return 'utf-8'


class OutputCapture:
    """
    输出捕获上下文管理器
    
    使用方式:
        async with OutputCapture() as capture:
            exec(code)
            async for chunk in capture.iter_output():
                yield chunk.to_sse()
    """
    
    def __init__(self, capture_stdout: bool = True, capture_stderr: bool = True):
        self.capture_stdout = capture_stdout
        self.capture_stderr = capture_stderr
        self.queue: asyncio.Queue[OutputChunk] = asyncio.Queue()
        
        self._original_stdout = None
        self._original_stderr = None
        self._stdout_capture = None
        self._stderr_capture = None
        self._finished = False
    
    def start(self):
        """开始捕获"""
        if self.capture_stdout:
            self._original_stdout = sys.stdout
            self._stdout_capture = StreamCapture(
                self.queue, OutputType.TEXT, self._original_stdout
            )
            sys.stdout = self._stdout_capture
        
        if self.capture_stderr:
            self._original_stderr = sys.stderr
            self._stderr_capture = StreamCapture(
                self.queue, OutputType.ERROR, self._original_stderr
            )
            sys.stderr = self._stderr_capture
    
    def stop(self):
        """停止捕获"""
        if self._original_stdout is not None:
            sys.stdout = self._original_stdout
            self._original_stdout = None
        
        if self._original_stderr is not None:
            sys.stderr = self._original_stderr
            self._original_stderr = None
        
        self._finished = True
    
    def put_output(self, output_type: OutputType, content: str):
        """手动放入输出"""
        chunk = OutputChunk(type=output_type, content=content)
        try:
            self.queue.put_nowait(chunk)
        except:
            pass
    
    def put_image(self, base64_data: str):
        """放入图片输出"""
        self.put_output(OutputType.IMAGE, base64_data)
    
    def put_result(self, result_json: str):
        """放入结果输出"""
        self.put_output(OutputType.RESULT, result_json)
    
    def drain_queue(self) -> list[OutputChunk]:
        """
        同步清空队列，返回所有待处理的输出
        
        用于在执行结束后获取所有剩余输出
        """
        chunks = []
        while True:
            try:
                chunk = self.queue.get_nowait()
                chunks.append(chunk)
            except asyncio.QueueEmpty:
                break
        return chunks
    
    async def iter_output(self, timeout: float = 0.1) -> AsyncIterator[OutputChunk]:
        """
        异步迭代输出
        
        Args:
            timeout: 队列获取超时时间
            
        Yields:
            输出片段
        """
        while not self._finished or not self.queue.empty():
            try:
                chunk = await asyncio.wait_for(
                    self.queue.get(), 
                    timeout=timeout
                )
                yield chunk
            except asyncio.TimeoutError:
                # 超时但未结束，继续等待
                if self._finished:
                    break
                continue
            except Exception:
                break
        
        # 清空剩余输出
        for chunk in self.drain_queue():
            yield chunk
    
    def __enter__(self):
        """同步上下文管理器入口"""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """同步上下文管理器出口"""
        self.stop()
        return False
    
    async def __aenter__(self):
        """异步上下文管理器入口"""
        self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        self.stop()
        return False

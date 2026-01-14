"""
Worker FastAPI 入口

提供代码执行、状态重置、健康检查等 HTTP 接口。
支持 SSE (Server-Sent Events) 流式输出。
"""

import asyncio
import logging
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from core.executor import IPythonExecutor


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("worker")


# 全局执行器实例（每个 Worker 容器一个）
executor: Optional[IPythonExecutor] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global executor
    
    # 启动时初始化执行器
    logger.info("Initializing IPython executor...")
    executor = IPythonExecutor()
    logger.info("IPython executor initialized successfully")
    
    yield
    
    # 关闭时清理
    logger.info("Shutting down worker...")
    executor = None


# 创建 FastAPI 应用
app = FastAPI(
    title="TableMind Worker",
    description="代码执行沙盒 Worker 服务",
    version="1.0.0",
    lifespan=lifespan
)

class ExecuteRequest(BaseModel):
    """代码执行请求"""
    code: str = Field(..., description="要执行的 Python 代码")
    result_var: Optional[str] = Field(None, description="需要返回的变量名")


class ResetResponse(BaseModel):
    """重置响应"""
    success: bool
    message: str


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    executor_ready: bool
    execution_count: int
    variables_count: int


@app.post("/exec")
async def execute_code(request: ExecuteRequest):
    """
    执行 Python 代码
    
    返回 SSE 流，包含执行过程中的输出和最终结果。
    
    SSE 数据格式:
    - `<txt>内容</txt>`: 标准输出
    - `<err>内容</err>`: 错误输出
    - `<img>base64</img>`: 图片
    - `<result>JSON</result>`: 执行结果
    """
    if executor is None:
        raise HTTPException(status_code=503, detail="Executor not initialized")
    
    logger.info(f"Executing code: {request.code[:100]}...")
    
    async def generate_sse():
        """生成 SSE 流"""
        try:
            async for chunk in executor.run_code(
                code=request.code,
                result_var=request.result_var
            ):
                # 转换为 SSE 格式
                sse_data = chunk.to_sse()
                yield f"data: {sse_data}\n\n"
        except asyncio.CancelledError:
            logger.warning("Execution cancelled")
            yield f"data: <err>Execution cancelled</err>\n\n"
        except Exception as e:
            logger.error(f"Execution error: {e}")
            yield f"data: <err>{str(e)}</err>\n\n"
    
    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # 禁用 nginx 缓冲
        }
    )


@app.post("/reset", response_model=ResetResponse)
async def reset_state():
    """
    重置 IPython 执行器状态
    
    清空所有用户定义的变量，恢复到初始状态。
    """
    if executor is None:
        raise HTTPException(status_code=503, detail="Executor not initialized")
    
    try:
        executor.reset()
        logger.info("Executor state reset successfully")
        return ResetResponse(
            success=True,
            message="Executor state reset successfully"
        )
    except Exception as e:
        logger.error(f"Reset failed: {e}")
        return ResetResponse(
            success=False,
            message=f"Reset failed: {str(e)}"
        )


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """
    健康检查
    
    返回 Worker 的健康状态和基本信息。
    """
    if executor is None:
        return HealthResponse(
            status="unhealthy",
            executor_ready=False,
            execution_count=0,
            variables_count=0
        )
    
    return HealthResponse(
        status="healthy",
        executor_ready=True,
        execution_count=executor.get_execution_count(),
        variables_count=len(executor.list_variables())
    )


@app.get("/variables")
async def list_variables():
    """
    列出当前所有用户定义的变量
    
    用于调试和监控。
    """
    if executor is None:
        raise HTTPException(status_code=503, detail="Executor not initialized")
    
    variables = list(executor.list_variables())
    return {
        "count": len(variables),
        "variables": variables
    }


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "TableMind Worker",
        "version": "1.0.0",
        "endpoints": {
            "execute": "POST /exec",
            "reset": "POST /reset",
            "health": "GET /health",
            "variables": "GET /variables"
        }
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=9000,
        reload=False,
        log_level="info"
    )


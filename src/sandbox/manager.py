"""
SandboxManager 主类

整合 DockerManager 和 SessionManager，提供统一的沙盒管理接口。
"""

import logging
import asyncio
from typing import Optional, AsyncIterator, Dict, Any

import httpx

from .models import SandboxConfig, SessionInfo, SessionState
from .session import SessionManager
from .docker_client import DockerManager

logger = logging.getLogger(__name__)


class SandboxManager:
    """
    沙盒管理器
    
    整合 Docker 容器管理和 Session 管理，提供统一的代码执行沙盒接口。
    
    主要功能：
    - 创建和管理 Session
    - 执行代码并流式返回结果
    - 管理 Worker 容器生命周期
    """
    
    def __init__(self, config: Optional[SandboxConfig] = None):
        """
        初始化沙盒管理器
        
        Args:
            config: 沙盒配置，如果不指定则从配置文件加载
        """
        self.config = config or SandboxConfig()
        self.docker_manager = DockerManager(self.config)
        self.session_manager = SessionManager()
        
        # Session 级别的锁，防止同一 Session 并发执行
        self._session_locks: Dict[str, asyncio.Lock] = {}
        
        # 容器计数器（用于生成唯一容器名）
        self._container_counter = 0
        self._counter_lock = asyncio.Lock()
    
    async def _get_next_container_name(self) -> str:
        """生成下一个容器名称"""
        async with self._counter_lock:
            self._container_counter += 1
            return f"{self.config.container_prefix}-{self._container_counter}"
    
    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """获取 Session 的锁"""
        if session_id not in self._session_locks:
            self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]
    
    async def create_session(
        self,
        volumes: Optional[Dict[str, Dict[str, str]]] = None
    ) -> str:
        """
        创建新的 Session
        
        这会启动一个新的 Worker 容器，并等待其健康检查通过。
        
        Args:
            volumes: 卷挂载配置
            
        Returns:
            session_id
            
        Raises:
            RuntimeError: 如果容器创建或启动失败
        """
        container_name = await self._get_next_container_name()
        session_id = SessionManager.generate_session_id()
        
        logger.info(f"Creating session {session_id} with container {container_name}")
        
        try:
            # 创建容器
            container = self.docker_manager.create_container(
                name=container_name,
                volumes=volumes
            )
            
            # 获取容器 IP（此时还不可用，先用占位符）
            container_id = container.id
            
            # 注册 Session（状态为 CREATING）
            session = await self.session_manager.create_session(
                container_id=container_id,
                container_ip="",  # 启动后更新
                session_id=session_id
            )
            
            # 启动容器
            self.docker_manager.start_container(container)
            
            # 获取容器 IP
            container_ip = self.docker_manager.get_container_ip(container)
            session.container_ip = container_ip
            
            # 等待健康检查
            is_healthy = await self.docker_manager.wait_for_healthy(container)
            
            if not is_healthy:
                # 清理失败的容器
                self.docker_manager.stop_container(container)
                self.docker_manager.remove_container(container)
                await self.session_manager.release_session(session_id)
                raise RuntimeError(f"Container {container_name} health check failed")
            
            # 更新状态为 READY
            await self.session_manager.update_state(session_id, SessionState.READY)
            
            logger.info(f"Session {session_id} is ready (container IP: {container_ip})")
            return session_id
            
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            # 确保清理
            await self.session_manager.release_session(session_id)
            raise
    
    async def execute(
        self,
        session_id: str,
        code: str,
        result_var: Optional[str] = None
    ) -> AsyncIterator[str]:
        """
        在指定 Session 中执行代码
        
        Args:
            session_id: Session ID
            code: 要执行的 Python 代码
            result_var: 需要返回的变量名
            
        Yields:
            SSE 格式的输出片段
            
        Raises:
            ValueError: 如果 Session 不存在或不可用
        """
        # 获取 Session
        session = await self.session_manager.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")
        
        if not session.is_available():
            raise ValueError(
                f"Session {session_id} is not available (state: {session.state.value})"
            )
        
        # 获取 Session 锁，防止并发执行
        lock = self._get_session_lock(session_id)
        
        async with lock:
            # 更新状态为 EXECUTING
            await self.session_manager.update_state(session_id, SessionState.EXECUTING)
            
            try:
                # 构建请求
                url = f"http://{session.container_ip}:{self.config.worker_port}/exec"
                payload = {
                    "code": code,
                    "result_var": result_var
                }
                
                # 发送请求并流式返回结果
                async with httpx.AsyncClient() as client:
                    # 禁用超时或设置很大的超时值
                    timeout = httpx.Timeout(
                        connect=10.0,
                        read=self.config.execution_timeout,
                        write=10.0,
                        pool=10.0
                    )
                    
                    async with client.stream(
                        "POST",
                        url,
                        json=payload,
                        timeout=timeout
                    ) as response:
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                yield line[6:]  # 去掉 "data: " 前缀
                
            except httpx.TimeoutException:
                logger.error(f"Execution timeout for session {session_id}")
                yield "<err>Execution timeout</err>"
            except Exception as e:
                logger.error(f"Execution error for session {session_id}: {e}")
                yield f"<err>{str(e)}</err>"
            finally:
                # 恢复状态为 READY
                await self.session_manager.update_state(session_id, SessionState.READY)
    
    async def release_session(self, session_id: str) -> bool:
        """
        释放 Session
        
        停止并删除关联的容器，从管理器中移除 Session。
        
        Args:
            session_id: Session ID
            
        Returns:
            是否成功释放
        """
        session = await self.session_manager.get_session(session_id)
        if session is None:
            logger.warning(f"Session {session_id} not found for release")
            return False
        
        logger.info(f"Releasing session {session_id}")
        
        # 更新状态为 DESTROYING
        await self.session_manager.update_state(session_id, SessionState.DESTROYING)
        
        # 停止并删除容器
        container = self.docker_manager.get_container(session.container_id)
        if container:
            self.docker_manager.stop_container(container)
            self.docker_manager.remove_container(container)
        
        # 从管理器中移除
        await self.session_manager.release_session(session_id)
        
        # 清理锁
        if session_id in self._session_locks:
            del self._session_locks[session_id]
        
        return True
    
    async def get_session_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        获取 Session 信息
        
        Args:
            session_id: Session ID
            
        Returns:
            Session 信息字典或 None
        """
        session = await self.session_manager.get_session(session_id)
        if session:
            return session.to_dict()
        return None
    
    async def list_sessions(self) -> list:
        """
        列出所有 Session
        
        Returns:
            Session 信息列表
        """
        sessions = await self.session_manager.get_all_sessions()
        return [s.to_dict() for s in sessions]
    
    async def cleanup_orphan_containers(self) -> int:
        """
        清理孤儿容器
        
        清理所有没有对应 Session 的 Worker 容器。
        通常在启动时调用。
        
        Returns:
            清理的容器数量
        """
        # 获取所有 Session 的容器 ID
        sessions = await self.session_manager.get_all_sessions()
        session_container_ids = {s.container_id for s in sessions}
        
        # 获取所有 Worker 容器
        containers = self.docker_manager.list_worker_containers()
        
        # 找出孤儿容器
        orphan_count = 0
        for container in containers:
            if container.id not in session_container_ids:
                logger.info(f"Cleaning up orphan container: {container.short_id}")
                self.docker_manager.stop_container(container)
                self.docker_manager.remove_container(container)
                orphan_count += 1
        
        if orphan_count > 0:
            logger.info(f"Cleaned up {orphan_count} orphan containers")
        
        return orphan_count
    
    async def initialize(self) -> None:
        """
        初始化沙盒管理器
        
        执行启动时的初始化操作：
        - 清理孤儿容器
        - 确保网络存在
        """
        logger.info("Initializing SandboxManager...")
        
        # 确保 Docker 连接正常
        if not self.docker_manager.ping():
            raise RuntimeError("Failed to connect to Docker daemon")
        
        # 确保网络存在
        self.docker_manager.ensure_network()
        
        # 清理孤儿容器
        await self.cleanup_orphan_containers()
        
        logger.info("SandboxManager initialized successfully")
    
    async def shutdown(self) -> None:
        """
        关闭沙盒管理器
        
        释放所有 Session，清理资源。
        """
        logger.info("Shutting down SandboxManager...")
        
        # 释放所有 Session
        sessions = await self.session_manager.get_all_sessions()
        for session in sessions:
            await self.release_session(session.session_id)
        
        logger.info("SandboxManager shutdown complete")


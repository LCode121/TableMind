"""
Session 管理模块

管理 Session 与 Worker 容器的映射关系。
"""

import uuid
import asyncio
import logging
from typing import Dict, Optional, List
from datetime import datetime

from .models import SessionInfo, SessionState

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Session 管理器
    
    管理 Session ID 与 Worker 容器的映射关系。
    支持线程安全的并发访问。
    """
    
    def __init__(self):
        """初始化 Session 管理器"""
        self._sessions: Dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()
    
    @staticmethod
    def generate_session_id() -> str:
        """生成唯一的 Session ID"""
        return str(uuid.uuid4())
    
    async def create_session(
        self,
        container_id: str,
        container_ip: str,
        session_id: Optional[str] = None
    ) -> SessionInfo:
        """
        创建并注册新的 Session
        
        Args:
            container_id: 容器 ID
            container_ip: 容器 IP 地址
            session_id: 可选的 Session ID（不指定则自动生成）
            
        Returns:
            创建的 SessionInfo
            
        Raises:
            ValueError: 如果指定的 session_id 已存在
        """
        if session_id is None:
            session_id = self.generate_session_id()
        
        async with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"Session {session_id} already exists")
            
            session = SessionInfo(
                session_id=session_id,
                container_id=container_id,
                container_ip=container_ip,
                state=SessionState.CREATING
            )
            
            self._sessions[session_id] = session
            logger.info(f"Created session: {session_id} -> container {container_id[:12]}")
            
            return session
    
    async def get_session(self, session_id: str) -> Optional[SessionInfo]:
        """
        获取 Session 信息
        
        Args:
            session_id: Session ID
            
        Returns:
            SessionInfo 或 None（如果不存在）
        """
        async with self._lock:
            return self._sessions.get(session_id)
    
    async def update_state(
        self,
        session_id: str,
        state: SessionState,
        error_message: Optional[str] = None
    ) -> bool:
        """
        更新 Session 状态
        
        Args:
            session_id: Session ID
            state: 新状态
            error_message: 错误信息（可选）
            
        Returns:
            是否更新成功
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                logger.warning(f"Session {session_id} not found for state update")
                return False
            
            old_state = session.state
            session.state = state
            session.update_last_used()
            
            if error_message:
                session.error_message = error_message
            
            logger.debug(f"Session {session_id} state: {old_state.value} -> {state.value}")
            return True
    
    async def release_session(self, session_id: str) -> Optional[SessionInfo]:
        """
        释放 Session（从管理器中移除）
        
        Args:
            session_id: Session ID
            
        Returns:
            被移除的 SessionInfo 或 None
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                session.state = SessionState.DESTROYED
                logger.info(f"Released session: {session_id}")
            return session
    
    async def get_all_sessions(self) -> List[SessionInfo]:
        """
        获取所有 Session 列表
        
        Returns:
            所有 Session 的副本列表
        """
        async with self._lock:
            return list(self._sessions.values())
    
    async def get_active_sessions(self) -> List[SessionInfo]:
        """
        获取所有活跃的 Session
        
        Returns:
            活跃状态的 Session 列表
        """
        async with self._lock:
            return [s for s in self._sessions.values() if s.is_active()]
    
    async def count(self) -> int:
        """获取 Session 总数"""
        async with self._lock:
            return len(self._sessions)
    
    async def count_active(self) -> int:
        """获取活跃 Session 数量"""
        async with self._lock:
            return sum(1 for s in self._sessions.values() if s.is_active())
    
    async def get_session_by_container_id(
        self,
        container_id: str
    ) -> Optional[SessionInfo]:
        """
        根据容器 ID 查找 Session
        
        Args:
            container_id: 容器 ID
            
        Returns:
            对应的 SessionInfo 或 None
        """
        async with self._lock:
            for session in self._sessions.values():
                if session.container_id == container_id:
                    return session
            return None


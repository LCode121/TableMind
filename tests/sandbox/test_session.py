"""
SessionManager 单元测试
"""

import pytest
import asyncio
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.sandbox.models import SessionState, SessionInfo
from src.sandbox.session import SessionManager


class TestSessionManager:
    """SessionManager 测试"""
    
    @pytest.fixture
    def manager(self):
        """创建 SessionManager 实例"""
        return SessionManager()
    
    @pytest.mark.asyncio
    async def test_generate_session_id(self):
        """测试生成 Session ID"""
        id1 = SessionManager.generate_session_id()
        id2 = SessionManager.generate_session_id()
        
        # ID 应该是唯一的
        assert id1 != id2
        # ID 应该是 UUID 格式
        assert len(id1) == 36
        assert id1.count('-') == 4
    
    @pytest.mark.asyncio
    async def test_create_session(self, manager):
        """测试创建 Session"""
        session = await manager.create_session(
            container_id="container-123",
            container_ip="172.17.0.2"
        )
        
        assert session is not None
        assert session.container_id == "container-123"
        assert session.container_ip == "172.17.0.2"
        assert session.state == SessionState.CREATING
        assert isinstance(session.session_id, str)
        assert len(session.session_id) == 36
    
    @pytest.mark.asyncio
    async def test_create_session_with_custom_id(self, manager):
        """测试使用自定义 ID 创建 Session"""
        custom_id = "my-custom-session-id"
        session = await manager.create_session(
            container_id="container-123",
            container_ip="172.17.0.2",
            session_id=custom_id
        )
        
        assert session.session_id == custom_id
    
    @pytest.mark.asyncio
    async def test_create_session_duplicate_id_raises(self, manager):
        """测试重复 ID 抛出异常"""
        custom_id = "duplicate-id"
        
        await manager.create_session(
            container_id="container-1",
            container_ip="172.17.0.2",
            session_id=custom_id
        )
        
        with pytest.raises(ValueError, match="already exists"):
            await manager.create_session(
                container_id="container-2",
                container_ip="172.17.0.3",
                session_id=custom_id
            )
    
    @pytest.mark.asyncio
    async def test_get_session(self, manager):
        """测试获取 Session"""
        session = await manager.create_session(
            container_id="container-123",
            container_ip="172.17.0.2"
        )
        
        retrieved = await manager.get_session(session.session_id)
        
        assert retrieved is not None
        assert retrieved.session_id == session.session_id
        assert retrieved.container_id == session.container_id
    
    @pytest.mark.asyncio
    async def test_get_session_not_found(self, manager):
        """测试获取不存在的 Session"""
        result = await manager.get_session("non-existent-id")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_update_state(self, manager):
        """测试更新 Session 状态"""
        session = await manager.create_session(
            container_id="container-123",
            container_ip="172.17.0.2"
        )
        
        # 初始状态为 CREATING
        assert session.state == SessionState.CREATING
        
        # 更新为 READY
        result = await manager.update_state(session.session_id, SessionState.READY)
        assert result is True
        
        # 验证状态已更新
        updated = await manager.get_session(session.session_id)
        assert updated.state == SessionState.READY
    
    @pytest.mark.asyncio
    async def test_update_state_with_error(self, manager):
        """测试更新状态时设置错误信息"""
        session = await manager.create_session(
            container_id="container-123",
            container_ip="172.17.0.2"
        )
        
        await manager.update_state(
            session.session_id,
            SessionState.ERROR,
            error_message="Container crashed"
        )
        
        updated = await manager.get_session(session.session_id)
        assert updated.state == SessionState.ERROR
        assert updated.error_message == "Container crashed"
    
    @pytest.mark.asyncio
    async def test_update_state_not_found(self, manager):
        """测试更新不存在的 Session"""
        result = await manager.update_state("non-existent", SessionState.READY)
        assert result is False
    
    @pytest.mark.asyncio
    async def test_release_session(self, manager):
        """测试释放 Session"""
        session = await manager.create_session(
            container_id="container-123",
            container_ip="172.17.0.2"
        )
        session_id = session.session_id
        
        # 释放 Session
        released = await manager.release_session(session_id)
        
        assert released is not None
        assert released.state == SessionState.DESTROYED
        
        # 验证 Session 已被移除
        result = await manager.get_session(session_id)
        assert result is None
    
    @pytest.mark.asyncio
    async def test_release_session_not_found(self, manager):
        """测试释放不存在的 Session"""
        result = await manager.release_session("non-existent")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_all_sessions(self, manager):
        """测试获取所有 Session"""
        # 创建多个 Session
        await manager.create_session("c1", "172.17.0.2")
        await manager.create_session("c2", "172.17.0.3")
        await manager.create_session("c3", "172.17.0.4")
        
        sessions = await manager.get_all_sessions()
        
        assert len(sessions) == 3
    
    @pytest.mark.asyncio
    async def test_get_active_sessions(self, manager):
        """测试获取活跃 Session"""
        # 创建 3 个 Session
        s1 = await manager.create_session("c1", "172.17.0.2")
        s2 = await manager.create_session("c2", "172.17.0.3")
        s3 = await manager.create_session("c3", "172.17.0.4")
        
        # 设置不同状态
        await manager.update_state(s1.session_id, SessionState.READY)
        await manager.update_state(s2.session_id, SessionState.EXECUTING)
        await manager.update_state(s3.session_id, SessionState.DESTROYED)
        
        active = await manager.get_active_sessions()
        
        # READY 和 EXECUTING 是活跃状态
        assert len(active) == 2
    
    @pytest.mark.asyncio
    async def test_count(self, manager):
        """测试 Session 计数"""
        assert await manager.count() == 0
        
        await manager.create_session("c1", "172.17.0.2")
        assert await manager.count() == 1
        
        await manager.create_session("c2", "172.17.0.3")
        assert await manager.count() == 2
    
    @pytest.mark.asyncio
    async def test_count_active(self, manager):
        """测试活跃 Session 计数"""
        s1 = await manager.create_session("c1", "172.17.0.2")
        s2 = await manager.create_session("c2", "172.17.0.3")
        
        # 初始状态都是 CREATING，不是活跃的
        assert await manager.count_active() == 0
        
        await manager.update_state(s1.session_id, SessionState.READY)
        assert await manager.count_active() == 1
        
        await manager.update_state(s2.session_id, SessionState.EXECUTING)
        assert await manager.count_active() == 2
    
    @pytest.mark.asyncio
    async def test_get_session_by_container_id(self, manager):
        """测试根据容器 ID 查找 Session"""
        await manager.create_session("container-abc", "172.17.0.2")
        await manager.create_session("container-xyz", "172.17.0.3")
        
        session = await manager.get_session_by_container_id("container-abc")
        
        assert session is not None
        assert session.container_id == "container-abc"
    
    @pytest.mark.asyncio
    async def test_get_session_by_container_id_not_found(self, manager):
        """测试根据不存在的容器 ID 查找"""
        result = await manager.get_session_by_container_id("non-existent")
        assert result is None
    
    @pytest.mark.asyncio
    async def test_concurrent_access(self, manager):
        """测试并发访问安全性"""
        async def create_and_update(i):
            session = await manager.create_session(f"container-{i}", f"172.17.0.{i}")
            await manager.update_state(session.session_id, SessionState.READY)
            return session.session_id
        
        # 并发创建 10 个 Session
        tasks = [create_and_update(i) for i in range(10)]
        session_ids = await asyncio.gather(*tasks)
        
        # 验证所有 Session 都被正确创建
        assert len(set(session_ids)) == 10
        assert await manager.count() == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


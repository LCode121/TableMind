"""
数据模型单元测试
"""

import pytest
from datetime import datetime, timedelta

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.sandbox.models import SessionState, SessionInfo, SandboxConfig


class TestSessionState:
    """SessionState 枚举测试"""
    
    def test_all_states_exist(self):
        """测试所有状态都存在"""
        assert SessionState.CREATING.value == "creating"
        assert SessionState.READY.value == "ready"
        assert SessionState.EXECUTING.value == "executing"
        assert SessionState.DESTROYING.value == "destroying"
        assert SessionState.DESTROYED.value == "destroyed"
        assert SessionState.ERROR.value == "error"
    
    def test_state_from_value(self):
        """测试从字符串值创建状态"""
        assert SessionState("ready") == SessionState.READY
        assert SessionState("executing") == SessionState.EXECUTING


class TestSessionInfo:
    """SessionInfo 数据类测试"""
    
    def test_create_session_info(self):
        """测试创建 SessionInfo"""
        session = SessionInfo(
            session_id="test-session-123",
            container_id="container-abc",
            container_ip="172.17.0.2",
            state=SessionState.READY
        )
        
        assert session.session_id == "test-session-123"
        assert session.container_id == "container-abc"
        assert session.container_ip == "172.17.0.2"
        assert session.state == SessionState.READY
        assert session.error_message is None
        assert isinstance(session.created_at, datetime)
        assert isinstance(session.last_used_at, datetime)
    
    def test_update_last_used(self):
        """测试更新最后使用时间"""
        session = SessionInfo(
            session_id="test",
            container_id="container",
            container_ip="172.17.0.2",
            state=SessionState.READY
        )
        
        old_time = session.last_used_at
        # 等待一小段时间
        import time
        time.sleep(0.01)
        
        session.update_last_used()
        
        assert session.last_used_at > old_time
    
    def test_is_active(self):
        """测试 is_active 方法"""
        # READY 状态应该是活跃的
        session = SessionInfo(
            session_id="test",
            container_id="container",
            container_ip="172.17.0.2",
            state=SessionState.READY
        )
        assert session.is_active() is True
        
        # EXECUTING 状态应该是活跃的
        session.state = SessionState.EXECUTING
        assert session.is_active() is True
        
        # CREATING 状态不是活跃的
        session.state = SessionState.CREATING
        assert session.is_active() is False
        
        # DESTROYED 状态不是活跃的
        session.state = SessionState.DESTROYED
        assert session.is_active() is False
    
    def test_is_available(self):
        """测试 is_available 方法"""
        session = SessionInfo(
            session_id="test",
            container_id="container",
            container_ip="172.17.0.2",
            state=SessionState.READY
        )
        
        # READY 状态可用
        assert session.is_available() is True
        
        # EXECUTING 状态不可用（正在执行中）
        session.state = SessionState.EXECUTING
        assert session.is_available() is False
    
    def test_to_dict(self):
        """测试转换为字典"""
        now = datetime.now()
        session = SessionInfo(
            session_id="test-123",
            container_id="container-abc",
            container_ip="172.17.0.2",
            state=SessionState.READY,
            created_at=now,
            last_used_at=now,
            error_message=None
        )
        
        result = session.to_dict()
        
        assert result["session_id"] == "test-123"
        assert result["container_id"] == "container-abc"
        assert result["container_ip"] == "172.17.0.2"
        assert result["state"] == "ready"
        assert result["error_message"] is None
        assert "created_at" in result
        assert "last_used_at" in result
    
    def test_to_dict_with_error(self):
        """测试带错误信息时的字典转换"""
        session = SessionInfo(
            session_id="test",
            container_id="container",
            container_ip="172.17.0.2",
            state=SessionState.ERROR,
            error_message="Container crashed"
        )
        
        result = session.to_dict()
        assert result["state"] == "error"
        assert result["error_message"] == "Container crashed"


class TestSandboxConfig:
    """SandboxConfig 数据类测试"""
    
    def test_default_values(self):
        """测试默认值"""
        config = SandboxConfig()
        
        assert config.enabled is True
        assert config.worker_image == "tablemind/worker:latest"
        assert config.worker_port == 9000
        assert config.memory_limit == "2g"
        assert config.cpu_limit == 1.0
        assert config.network_name == "tablemind-network"
        assert config.container_prefix == "tablemind-worker"
        assert config.health_check_timeout == 30
        assert config.health_check_interval == 1.0
        assert config.execution_timeout == 300
        assert config.data_mount_path == "/data"
    
    def test_from_dict(self):
        """测试从字典创建配置"""
        config_dict = {
            "enabled": False,
            "worker_image": "custom/worker:v2",
            "worker_port": 8080,
            "memory_limit": "4g",
            "cpu_limit": 2.0,
            "execution_timeout": 600,
        }
        
        config = SandboxConfig.from_dict(config_dict)
        
        assert config.enabled is False
        assert config.worker_image == "custom/worker:v2"
        assert config.worker_port == 8080
        assert config.memory_limit == "4g"
        assert config.cpu_limit == 2.0
        assert config.execution_timeout == 600
        # 未指定的使用默认值
        assert config.network_name == "tablemind-network"
    
    def test_from_empty_dict(self):
        """测试从空字典创建（使用全部默认值）"""
        config = SandboxConfig.from_dict({})
        
        assert config.enabled is True
        assert config.worker_image == "tablemind/worker:latest"
        assert config.worker_port == 9000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


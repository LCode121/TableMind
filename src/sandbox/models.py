"""
沙盒数据模型

定义 Session 状态、Session 信息和配置等数据结构。
"""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class SessionState(Enum):
    """Session 状态枚举"""
    CREATING = "creating"      # 正在创建容器
    READY = "ready"            # 就绪，等待执行
    EXECUTING = "executing"    # 正在执行代码
    DESTROYING = "destroying"  # 正在销毁
    DESTROYED = "destroyed"    # 已销毁
    ERROR = "error"            # 错误状态


@dataclass
class SessionInfo:
    """
    Session 信息
    
    存储 Session 与 Worker 容器的映射关系及状态。
    """
    session_id: str
    container_id: str
    container_ip: str
    state: SessionState
    created_at: datetime = field(default_factory=datetime.now)
    last_used_at: datetime = field(default_factory=datetime.now)
    error_message: Optional[str] = None
    
    def update_last_used(self) -> None:
        """更新最后使用时间"""
        self.last_used_at = datetime.now()
    
    def is_active(self) -> bool:
        """检查 Session 是否活跃（可用于执行）"""
        return self.state in (SessionState.READY, SessionState.EXECUTING)
    
    def is_available(self) -> bool:
        """检查 Session 是否可用于新的执行请求"""
        return self.state == SessionState.READY
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "session_id": self.session_id,
            "container_id": self.container_id,
            "container_ip": self.container_ip,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
            "last_used_at": self.last_used_at.isoformat(),
            "error_message": self.error_message,
        }


@dataclass
class SandboxConfig:
    """
    沙盒配置
    
    从 config.yaml 中加载的沙盒相关配置。
    """
    enabled: bool = True
    worker_image: str = "tablemind/worker:latest"
    worker_port: int = 9000
    memory_limit: str = "2g"
    cpu_limit: float = 1.0
    network_name: str = "tablemind-network"
    container_prefix: str = "tablemind-worker"
    health_check_timeout: int = 30
    health_check_interval: float = 1.0
    execution_timeout: int = 300
    data_mount_path: str = "/data"
    
    @classmethod
    def from_dict(cls, config: dict) -> "SandboxConfig":
        """从字典创建配置"""
        return cls(
            enabled=config.get("enabled", True),
            worker_image=config.get("worker_image", "tablemind/worker:latest"),
            worker_port=config.get("worker_port", 9000),
            memory_limit=config.get("memory_limit", "2g"),
            cpu_limit=config.get("cpu_limit", 1.0),
            network_name=config.get("network_name", "tablemind-network"),
            container_prefix=config.get("container_prefix", "tablemind-worker"),
            health_check_timeout=config.get("health_check_timeout", 30),
            health_check_interval=config.get("health_check_interval", 1.0),
            execution_timeout=config.get("execution_timeout", 300),
            data_mount_path=config.get("data_mount_path", "/data"),
        )
    
    @classmethod
    def load_from_config(cls) -> "SandboxConfig":
        """从项目配置文件加载"""
        from src.config import get_config
        config = get_config()
        sandbox_config = config.get("sandbox", {})
        return cls.from_dict(sandbox_config)


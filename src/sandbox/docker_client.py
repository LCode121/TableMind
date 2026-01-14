"""
Docker 客户端封装

提供 Docker 容器管理功能，包括创建、启动、停止、删除容器等。
"""

import time
import logging
import asyncio
from typing import Optional, Dict, Any, List

import docker
from docker.models.containers import Container
from docker.errors import NotFound, APIError

from .models import SandboxConfig

logger = logging.getLogger(__name__)


class DockerManager:
    """
    Docker 管理器
    
    封装 docker-py 操作，管理 Worker 容器的生命周期。
    """
    
    def __init__(self, config: Optional[SandboxConfig] = None):
        """
        初始化 Docker 管理器
        
        Args:
            config: 沙盒配置，如果不指定则使用默认配置
        """
        self.config = config or SandboxConfig()
        self._client: Optional[docker.DockerClient] = None
        self._network_id: Optional[str] = None
    
    @property
    def client(self) -> docker.DockerClient:
        """获取 Docker 客户端（懒加载）"""
        if self._client is None:
            self._client = docker.from_env()
            logger.info("Docker client initialized")
        return self._client
    
    def ensure_network(self) -> str:
        """
        确保内部网络存在
        
        Returns:
            网络 ID
        """
        if self._network_id:
            return self._network_id
        
        network_name = self.config.network_name
        
        try:
            # 检查网络是否已存在
            network = self.client.networks.get(network_name)
            self._network_id = network.id
            logger.debug(f"Using existing network: {network_name}")
        except NotFound:
            # 创建新网络
            network = self.client.networks.create(
                name=network_name,
                driver="bridge",
                internal=False  # 阶段 2 允许外网访问，阶段 4 再改为 True
            )
            self._network_id = network.id
            logger.info(f"Created network: {network_name}")
        
        return self._network_id
    
    def create_container(
        self,
        name: str,
        volumes: Optional[Dict[str, Dict[str, str]]] = None,
        environment: Optional[Dict[str, str]] = None
    ) -> Container:
        """
        创建 Worker 容器（不启动）
        
        Args:
            name: 容器名称
            volumes: 卷挂载配置 {host_path: {'bind': container_path, 'mode': 'ro'}}
            environment: 环境变量
            
        Returns:
            创建的容器对象
        """
        # 确保网络存在
        self.ensure_network()
        
        # 构建容器配置
        container_config = {
            "image": self.config.worker_image,
            "name": name,
            "detach": True,
            "network": self.config.network_name,
            "mem_limit": self.config.memory_limit,
            "cpu_quota": int(self.config.cpu_limit * 100000),
            "pids_limit": 100,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }
        
        if volumes:
            container_config["volumes"] = volumes
        
        if environment:
            container_config["environment"] = environment
        
        container = self.client.containers.create(**container_config)
        logger.info(f"Created container: {name} ({container.short_id})")
        
        return container
    
    def start_container(self, container: Container) -> None:
        """
        启动容器
        
        Args:
            container: 容器对象
        """
        container.start()
        logger.info(f"Started container: {container.name} ({container.short_id})")
    
    def get_container_ip(self, container: Container) -> str:
        """
        获取容器的 IP 地址
        
        Args:
            container: 容器对象
            
        Returns:
            容器 IP 地址
            
        Raises:
            RuntimeError: 如果无法获取 IP
        """
        # 重新加载容器信息
        container.reload()
        
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        
        # 优先从指定网络获取
        if self.config.network_name in networks:
            ip = networks[self.config.network_name].get("IPAddress")
            if ip:
                return ip
        
        # 尝试从任意网络获取
        for network_name, network_info in networks.items():
            ip = network_info.get("IPAddress")
            if ip:
                return ip
        
        raise RuntimeError(f"Failed to get IP for container {container.short_id}")
    
    async def wait_for_healthy(
        self,
        container: Container,
        timeout: Optional[float] = None
    ) -> bool:
        """
        等待容器健康检查通过
        
        Args:
            container: 容器对象
            timeout: 超时时间（秒），None 则使用配置中的默认值
            
        Returns:
            是否健康
        """
        import httpx
        
        timeout = timeout or self.config.health_check_timeout
        interval = self.config.health_check_interval
        
        container_ip = self.get_container_ip(container)
        health_url = f"http://{container_ip}:{self.config.worker_port}/health"
        
        start_time = time.time()
        last_error = None
        
        async with httpx.AsyncClient() as client:
            while time.time() - start_time < timeout:
                try:
                    response = await client.get(health_url, timeout=5.0)
                    if response.status_code == 200:
                        logger.info(
                            f"Container {container.short_id} is healthy "
                            f"({time.time() - start_time:.1f}s)"
                        )
                        return True
                except Exception as e:
                    last_error = e
                
                await asyncio.sleep(interval)
        
        logger.error(
            f"Container {container.short_id} health check timeout "
            f"after {timeout}s: {last_error}"
        )
        return False
    
    def stop_container(
        self,
        container: Container,
        timeout: int = 10
    ) -> None:
        """
        停止容器
        
        Args:
            container: 容器对象
            timeout: 等待停止的超时时间
        """
        try:
            container.stop(timeout=timeout)
            logger.info(f"Stopped container: {container.short_id}")
        except Exception as e:
            logger.warning(f"Error stopping container {container.short_id}: {e}")
    
    def remove_container(
        self,
        container: Container,
        force: bool = True
    ) -> None:
        """
        删除容器
        
        Args:
            container: 容器对象
            force: 是否强制删除
        """
        try:
            container.remove(force=force, v=True)  # v=True 删除关联的匿名卷
            logger.info(f"Removed container: {container.short_id}")
        except Exception as e:
            logger.warning(f"Error removing container {container.short_id}: {e}")
    
    def get_container(self, container_id: str) -> Optional[Container]:
        """
        根据 ID 获取容器
        
        Args:
            container_id: 容器 ID
            
        Returns:
            容器对象或 None
        """
        try:
            return self.client.containers.get(container_id)
        except NotFound:
            return None
    
    def list_worker_containers(self) -> List[Container]:
        """
        列出所有 Worker 容器
        
        Returns:
            Worker 容器列表
        """
        prefix = self.config.container_prefix
        containers = self.client.containers.list(
            all=True,
            filters={"name": prefix}
        )
        return containers
    
    def cleanup_containers(self, container_ids: Optional[List[str]] = None) -> int:
        """
        清理容器
        
        Args:
            container_ids: 要清理的容器 ID 列表，None 则清理所有 Worker 容器
            
        Returns:
            清理的容器数量
        """
        count = 0
        
        if container_ids:
            for container_id in container_ids:
                container = self.get_container(container_id)
                if container:
                    self.stop_container(container)
                    self.remove_container(container)
                    count += 1
        else:
            for container in self.list_worker_containers():
                self.stop_container(container)
                self.remove_container(container)
                count += 1
        
        if count > 0:
            logger.info(f"Cleaned up {count} containers")
        
        return count
    
    def ping(self) -> bool:
        """
        测试 Docker 连接
        
        Returns:
            是否连接成功
        """
        try:
            self.client.ping()
            return True
        except Exception as e:
            logger.error(f"Docker ping failed: {e}")
            return False
    
    def get_info(self) -> Dict[str, Any]:
        """
        获取 Docker 信息
        
        Returns:
            Docker 系统信息
        """
        return self.client.info()


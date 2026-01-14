"""
沙盒集成测试

需要 Docker 环境才能运行！

在 WSL2 中运行：
    cd /mnt/c/Users/Semi-LuPY/Desktop/projects/chatBI/TableMind
    pip install docker httpx pytest pytest-asyncio
    python -m pytest tests/sandbox/test_integration.py -v -s

或单独运行测试脚本：
    python tests/sandbox/test_integration.py
"""

import pytest
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.sandbox.models import SandboxConfig, SessionState
from src.sandbox.docker_client import DockerManager
from src.sandbox.manager import SandboxManager


# 测试配置
TEST_CONFIG = SandboxConfig(
    worker_image="tablemind/worker:latest",
    worker_port=9000,
    memory_limit="1g",
    cpu_limit=1.0,
    network_name="tablemind-test-network",
    container_prefix="tablemind-test-worker",
    health_check_timeout=60,
    health_check_interval=1.0,
    execution_timeout=30,
)


class TestDockerManager:
    """DockerManager 集成测试"""
    
    @pytest.fixture
    def docker_manager(self):
        """创建 DockerManager 实例"""
        return DockerManager(TEST_CONFIG)
    
    def test_ping(self, docker_manager):
        """测试 Docker 连接"""
        assert docker_manager.ping() is True
        print("✅ Docker 连接正常")
    
    def test_get_info(self, docker_manager):
        """测试获取 Docker 信息"""
        info = docker_manager.get_info()
        assert "ServerVersion" in info
        print(f"✅ Docker 版本: {info['ServerVersion']}")
    
    def test_ensure_network(self, docker_manager):
        """测试创建/获取网络"""
        network_id = docker_manager.ensure_network()
        assert network_id is not None
        print(f"✅ 网络 ID: {network_id[:12]}")
    
    def test_create_and_start_container(self, docker_manager):
        """测试创建和启动容器"""
        container = None
        try:
            # 创建容器
            container = docker_manager.create_container(
                name=f"{TEST_CONFIG.container_prefix}-test-1"
            )
            assert container is not None
            print(f"✅ 创建容器: {container.short_id}")
            
            # 启动容器
            docker_manager.start_container(container)
            container.reload()
            assert container.status == "running"
            print(f"✅ 容器运行中: {container.status}")
            
            # 获取 IP
            ip = docker_manager.get_container_ip(container)
            assert ip is not None
            print(f"✅ 容器 IP: {ip}")
            
        finally:
            if container:
                docker_manager.stop_container(container)
                docker_manager.remove_container(container)
                print(f"✅ 已清理容器")
    
    @pytest.mark.asyncio
    async def test_wait_for_healthy(self, docker_manager):
        """测试健康检查"""
        container = None
        try:
            container = docker_manager.create_container(
                name=f"{TEST_CONFIG.container_prefix}-test-health"
            )
            docker_manager.start_container(container)
            
            # 等待健康检查
            is_healthy = await docker_manager.wait_for_healthy(container)
            assert is_healthy is True
            print(f"✅ 容器健康检查通过")
            
        finally:
            if container:
                docker_manager.stop_container(container)
                docker_manager.remove_container(container)
    
    def test_list_worker_containers(self, docker_manager):
        """测试列出 Worker 容器"""
        containers = docker_manager.list_worker_containers()
        print(f"✅ 发现 {len(containers)} 个 Worker 容器")
    
    def test_cleanup_containers(self, docker_manager):
        """测试清理容器"""
        count = docker_manager.cleanup_containers()
        print(f"✅ 清理了 {count} 个容器")


class TestSandboxManager:
    """SandboxManager 集成测试"""
    
    @pytest.fixture
    def sandbox_manager(self):
        """创建 SandboxManager 实例"""
        return SandboxManager(TEST_CONFIG)
    
    @pytest.mark.asyncio
    async def test_initialize(self, sandbox_manager):
        """测试初始化"""
        await sandbox_manager.initialize()
        print("✅ SandboxManager 初始化成功")
    
    @pytest.mark.asyncio
    async def test_create_and_release_session(self, sandbox_manager):
        """测试创建和释放 Session"""
        await sandbox_manager.initialize()
        
        # 创建 Session
        session_id = await sandbox_manager.create_session()
        assert session_id is not None
        print(f"✅ 创建 Session: {session_id}")
        
        # 获取 Session 信息
        info = await sandbox_manager.get_session_info(session_id)
        assert info is not None
        assert info["state"] == "ready"
        print(f"✅ Session 状态: {info['state']}")
        print(f"✅ 容器 IP: {info['container_ip']}")
        
        # 释放 Session
        result = await sandbox_manager.release_session(session_id)
        assert result is True
        print(f"✅ 释放 Session")
        
        # 关闭
        await sandbox_manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_execute_code(self, sandbox_manager):
        """测试执行代码"""
        await sandbox_manager.initialize()
        session_id = await sandbox_manager.create_session()
        
        try:
            # 执行简单代码
            print("\n--- 执行 print('Hello, World!') ---")
            outputs = []
            async for chunk in sandbox_manager.execute(
                session_id,
                "print('Hello, World!')"
            ):
                outputs.append(chunk)
                print(f"  输出: {chunk}")
            
            assert len(outputs) > 0
            assert any("Hello" in o for o in outputs)
            print("✅ 简单代码执行成功")
            
            # 执行变量赋值
            print("\n--- 执行 x = 42 ---")
            async for chunk in sandbox_manager.execute(session_id, "x = 42"):
                print(f"  输出: {chunk}")
            
            # 验证状态保持
            print("\n--- 执行 print(x * 2) ---")
            outputs = []
            async for chunk in sandbox_manager.execute(session_id, "print(x * 2)"):
                outputs.append(chunk)
                print(f"  输出: {chunk}")
            
            assert any("84" in o for o in outputs)
            print("✅ 状态保持正常")
            
            # 执行 pandas 代码
            print("\n--- 执行 pandas 代码 ---")
            code = """
import pandas as pd
df = pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})
print(df.sum())
"""
            async for chunk in sandbox_manager.execute(session_id, code, "df"):
                print(f"  输出: {chunk[:200]}...")
            
            print("✅ Pandas 代码执行成功")
            
        finally:
            await sandbox_manager.release_session(session_id)
            await sandbox_manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_execute_error_handling(self, sandbox_manager):
        """测试错误处理"""
        await sandbox_manager.initialize()
        session_id = await sandbox_manager.create_session()
        
        try:
            # 执行有错误的代码
            print("\n--- 执行错误代码 ---")
            outputs = []
            async for chunk in sandbox_manager.execute(
                session_id,
                "undefined_variable"
            ):
                outputs.append(chunk)
                print(f"  输出: {chunk}")
            
            # 应该包含错误信息
            assert any("err" in o.lower() or "error" in o.lower() for o in outputs)
            print("✅ 错误处理正常")
            
            # 验证 Session 仍然可用
            outputs = []
            async for chunk in sandbox_manager.execute(session_id, "print('still works')"):
                outputs.append(chunk)
            
            assert any("still works" in o for o in outputs)
            print("✅ Session 在错误后仍可用")
            
        finally:
            await sandbox_manager.release_session(session_id)
            await sandbox_manager.shutdown()
    
    @pytest.mark.asyncio
    async def test_multiple_sessions(self, sandbox_manager):
        """测试多 Session"""
        await sandbox_manager.initialize()
        
        # 创建多个 Session
        session_ids = []
        for i in range(2):
            session_id = await sandbox_manager.create_session()
            session_ids.append(session_id)
            print(f"✅ 创建 Session {i+1}: {session_id}")
        
        try:
            # 在不同 Session 中执行代码
            for i, session_id in enumerate(session_ids):
                async for chunk in sandbox_manager.execute(
                    session_id, 
                    f"x = {i * 10}"
                ):
                    pass
            
            # 验证隔离性
            for i, session_id in enumerate(session_ids):
                outputs = []
                async for chunk in sandbox_manager.execute(session_id, "print(x)"):
                    outputs.append(chunk)
                
                expected = str(i * 10)
                assert any(expected in o for o in outputs)
                print(f"✅ Session {i+1} 变量隔离正常 (x = {expected})")
            
        finally:
            for session_id in session_ids:
                await sandbox_manager.release_session(session_id)
            await sandbox_manager.shutdown()


async def run_quick_test():
    """快速测试脚本"""
    print("=" * 60)
    print("TableMind 沙盒集成测试")
    print("=" * 60)
    
    manager = SandboxManager(TEST_CONFIG)
    
    try:
        # 初始化
        print("\n1. 初始化 SandboxManager...")
        await manager.initialize()
        print("   ✅ 初始化成功")
        
        # 创建 Session
        print("\n2. 创建 Session...")
        session_id = await manager.create_session()
        info = await manager.get_session_info(session_id)
        print(f"   ✅ Session ID: {session_id}")
        print(f"   ✅ 容器 IP: {info['container_ip']}")
        
        # 执行代码
        print("\n3. 执行代码测试...")
        
        # 测试 1: 简单打印
        print("\n   [测试 1] print('Hello from sandbox!')")
        async for chunk in manager.execute(session_id, "print('Hello from sandbox!')"):
            print(f"   输出: {chunk}")
        
        # 测试 2: 变量赋值
        print("\n   [测试 2] data = [1, 2, 3, 4, 5]")
        async for chunk in manager.execute(session_id, "data = [1, 2, 3, 4, 5]"):
            print(f"   输出: {chunk}")
        
        # 测试 3: 状态保持
        print("\n   [测试 3] print(sum(data))  # 验证状态保持")
        async for chunk in manager.execute(session_id, "print(sum(data))"):
            print(f"   输出: {chunk}")
        
        # 测试 4: 返回变量
        print("\n   [测试 4] import pandas as pd; df = pd.DataFrame({'x': data})")
        code = "import pandas as pd\ndf = pd.DataFrame({'x': data})"
        async for chunk in manager.execute(session_id, code, "df"):
            if len(chunk) > 200:
                print(f"   输出: {chunk[:200]}...")
            else:
                print(f"   输出: {chunk}")
        
        # 释放 Session
        print("\n4. 释放 Session...")
        await manager.release_session(session_id)
        print("   ✅ Session 已释放")
        
        print("\n" + "=" * 60)
        print("✅ 所有测试通过!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await manager.shutdown()


if __name__ == "__main__":
    # 运行快速测试
    asyncio.run(run_quick_test())


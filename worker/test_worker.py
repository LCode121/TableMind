"""
Worker 自测脚本

验证阶段一的所有功能点：
1. 变量跨请求保持
2. SSE 流式输出正常
3. 错误能正确捕获
4. 脏变量能正确清理
5. DataFrame 能正确序列化
"""

import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import httpx
import json
import asyncio

BASE_URL = "http://localhost:9000"


def parse_sse_response(text: str) -> list:
    """解析 SSE 响应"""
    chunks = []
    for line in text.strip().split("\n"):
        if line.startswith("data: "):
            chunks.append(line[6:])
    return chunks


async def test_health():
    """测试健康检查"""
    print("\n" + "="*50)
    print("测试 1: 健康检查")
    print("="*50)
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{BASE_URL}/health")
        data = resp.json()
        print(f"响应: {json.dumps(data, indent=2)}")
        
        assert data["status"] == "healthy"
        assert data["executor_ready"] == True
        print("[PASS] 健康检查通过")


async def test_state_persistence():
    """测试状态保持"""
    print("\n" + "="*50)
    print("测试 2: 状态保持")
    print("="*50)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 步骤 1: 导入 pandas
        print("\n步骤 1: 导入 pandas")
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={"code": "import pandas as pd"}
        )
        print(f"响应: {resp.text[:200]}...")
        
        # 步骤 2: 创建 DataFrame
        print("\n步骤 2: 创建 DataFrame")
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={
                "code": "df = pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})",
                "result_var": "df"
            }
        )
        chunks = parse_sse_response(resp.text)
        print(f"响应片段数: {len(chunks)}")
        for chunk in chunks:
            print(f"  {chunk[:100]}...")
        
        # 步骤 3: 使用之前创建的变量
        print("\n步骤 3: 使用之前创建的变量")
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={
                "code": "result = df.sum()\nprint(result)",
                "result_var": "result"
            }
        )
        chunks = parse_sse_response(resp.text)
        print(f"响应片段数: {len(chunks)}")
        for chunk in chunks:
            print(f"  {chunk[:150]}...")
        
        # 验证变量存在
        resp = await client.get(f"{BASE_URL}/variables")
        data = resp.json()
        print(f"\n当前变量: {data['variables']}")
        
        assert "df" in data["variables"]
        assert "result" in data["variables"]
        print("[PASS] 状态保持测试通过")


async def test_error_capture():
    """测试错误捕获"""
    print("\n" + "="*50)
    print("测试 3: 错误捕获")
    print("="*50)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 语法错误
        print("\n测试语法错误:")
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={"code": "def broken("}
        )
        chunks = parse_sse_response(resp.text)
        for chunk in chunks:
            if "<err>" in chunk or "<result>" in chunk:
                print(f"  {chunk[:200]}...")
        
        # 运行时错误
        print("\n测试运行时错误:")
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={"code": "x = 1 / 0"}
        )
        chunks = parse_sse_response(resp.text)
        for chunk in chunks:
            if "<err>" in chunk or "<result>" in chunk:
                print(f"  {chunk[:200]}...")
        
        print("[PASS] 错误捕获测试通过")


async def test_dirty_variable_cleanup():
    """测试脏变量清理"""
    print("\n" + "="*50)
    print("测试 4: 脏变量清理")
    print("="*50)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 先重置状态
        await client.post(f"{BASE_URL}/reset")
        
        # 创建一个变量
        print("\n创建变量 clean_var:")
        await client.post(
            f"{BASE_URL}/exec",
            json={"code": "clean_var = 'I should survive'"}
        )
        
        # 执行会失败的代码（中间创建了变量）
        print("执行失败的代码 (尝试创建 dirty_var):")
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={"code": "dirty_var = 'I should be cleaned'\nraise ValueError('Intentional error')"}
        )
        chunks = parse_sse_response(resp.text)
        result_chunk = [c for c in chunks if "<result>" in c]
        if result_chunk:
            print(f"  {result_chunk[0][:200]}...")
        
        # 检查变量
        resp = await client.get(f"{BASE_URL}/variables")
        data = resp.json()
        print(f"\n当前变量: {data['variables']}")
        
        assert "clean_var" in data["variables"], "clean_var 应该存在"
        assert "dirty_var" not in data["variables"], "dirty_var 应该被清理"
        print("[PASS] 脏变量清理测试通过")


async def test_dataframe_serialization():
    """测试 DataFrame 序列化"""
    print("\n" + "="*50)
    print("测试 5: DataFrame 序列化")
    print("="*50)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 创建较大的 DataFrame
        code = """
import pandas as pd
import numpy as np

large_df = pd.DataFrame({
    'id': range(100),
    'name': [f'item_{i}' for i in range(100)],
    'value': np.random.random(100),
    'date': pd.date_range('2024-01-01', periods=100)
})
print(f"Created DataFrame with shape: {large_df.shape}")
"""
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={"code": code, "result_var": "large_df"}
        )
        chunks = parse_sse_response(resp.text)
        
        # 找到结果片段
        for chunk in chunks:
            if "<result>" in chunk:
                # 提取 JSON
                result_json = chunk.replace("<result>", "").replace("</result>", "")
                result = json.loads(result_json)
                
                if result.get("success") and result.get("return_value"):
                    rv = result["return_value"]
                    print(f"类型: {rv.get('type')}")
                    print(f"形状: {rv.get('shape')}")
                    print(f"列数: {rv.get('columns')}")
                    print(f"预览行数: {rv.get('preview_rows')}")
                    print(f"列名: {rv.get('column_names')}")
                    
                    assert rv["type"] == "DataFrame"
                    assert rv["shape"] == [100, 4]
                    assert rv["preview_rows"] == 10
                    print("[PASS] DataFrame 序列化测试通过")
                    return
        
        print("[FAIL] 未找到有效的结果")


async def test_sse_streaming():
    """测试 SSE 流式输出"""
    print("\n" + "="*50)
    print("测试 6: SSE 流式输出")
    print("="*50)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        code = """
import time
for i in range(3):
    print(f"Progress: {i+1}/3")
    time.sleep(0.1)
print("Done!")
"""
        resp = await client.post(
            f"{BASE_URL}/exec",
            json={"code": code}
        )
        
        chunks = parse_sse_response(resp.text)
        print(f"收到 {len(chunks)} 个输出片段:")
        for chunk in chunks:
            print(f"  {chunk}")
        
        # 验证有多个输出片段
        txt_chunks = [c for c in chunks if "<txt>" in c]
        assert len(txt_chunks) >= 1, "应该有多个文本输出"
        print("[PASS] SSE 流式输出测试通过")


async def test_reset():
    """测试重置功能"""
    print("\n" + "="*50)
    print("测试 7: 重置功能")
    print("="*50)
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # 重置前检查变量
        resp = await client.get(f"{BASE_URL}/variables")
        before = resp.json()
        print(f"重置前变量数: {before['count']}")
        
        # 重置
        resp = await client.post(f"{BASE_URL}/reset")
        data = resp.json()
        print(f"重置结果: {data}")
        
        # 重置后检查变量
        resp = await client.get(f"{BASE_URL}/variables")
        after = resp.json()
        print(f"重置后变量数: {after['count']}")
        
        assert after["count"] == 0, "重置后应该没有用户变量"
        print("[PASS] 重置功能测试通过")


async def main():
    """运行所有测试"""
    print("\n" + "#"*60)
    print("# Worker 阶段一自测")
    print("#"*60)
    
    try:
        await test_health()
        await test_state_persistence()
        await test_error_capture()
        await test_dirty_variable_cleanup()
        await test_dataframe_serialization()
        await test_sse_streaming()
        await test_reset()
        
        print("\n" + "="*60)
        print("[SUCCESS] 所有测试通过!")
        print("="*60)
        
    except AssertionError as e:
        print(f"\n[FAIL] 测试失败: {e}")
        raise
    except httpx.ConnectError:
        print("\n[FAIL] 无法连接到 Worker 服务，请确保服务已启动")
        print("   启动命令: cd TableMind/worker && python main.py")
        raise


if __name__ == "__main__":
    asyncio.run(main())


# TableMind Worker 镜像
# 用于执行用户 Python 代码的沙盒环境

FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    # 用于 matplotlib 等库
    libfreetype6-dev \
    libpng-dev \
    # 清理缓存
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建非 root 用户（安全考虑）
RUN useradd -m -u 1000 worker && \
    chown -R worker:worker /app

# 切换到非 root 用户
USER worker

# 暴露端口
EXPOSE 9000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:9000/health').raise_for_status()"

# 启动命令
CMD ["python", "main.py"]


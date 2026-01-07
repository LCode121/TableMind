# 基础镜像：选择官方的 Python 镜像（推荐指定具体版本，避免兼容性问题）
# slim 版本体积更小，适合生产环境；如果需要编译依赖，可改用 python:3.11（完整版）
FROM python:3.10-slim

# 设置工作目录（容器内的目录，后续操作都基于此）
WORKDIR /app

# 设置 Python 环境变量，避免生成 .pyc 文件，且输出直接打印到控制台
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 安装系统依赖（可选，若你的 Python 包需要编译，比如 psycopg2、pandas 等，需添加）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 先复制 requirements.txt，利用 Docker 缓存机制（修改代码不重新安装依赖）
COPY requirements.txt .

# 安装 Python 依赖（升级 pip，避免安装失败；--no-cache-dir 减小镜像体积）
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 复制项目所有代码到容器的工作目录（如果只需安装依赖，可注释此行）
# COPY . .

# 容器启动命令（可选，根据你的项目调整，比如启动 Flask 服务）
# CMD ["python", "app.py"]
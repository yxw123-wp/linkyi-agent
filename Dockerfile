# Hugging Face Spaces - Docker SDK
# 链弈融通智能体系统 (Flask + Qwen-Plus)
# 部署后可获得固定公网链接，无需本地开机，评委直接访问

FROM python:3.11-slim

# 设置时区与编码环境
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PORT=7860 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 安装系统依赖（最小化）
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先安装依赖（利用 Docker 层缓存）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app.py /app/app.py
COPY templates /app/templates

# Hugging Face Spaces 要求非 root 用户运行（UID 固定为 1000）
RUN useradd -m -u 1000 appuser \
    && mkdir -p /data /tmp \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 7860

# 单 worker + 多 threads：保证会话内状态一致；timeout=120 兼容 Qwen API 推理耗时
CMD ["gunicorn", "app:app", \
     "--workers=1", "--threads=4", \
     "--timeout=120", "--graceful-timeout=30", \
     "--bind=0.0.0.0:7860", \
     "--access-logfile=-", "--error-logfile=-"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

# 默认使用国内镜像加速；海外环境可覆盖为 https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt ./
RUN pip install --no-cache-dir --retries 10 --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY app ./app
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8000

CMD ["./entrypoint.sh", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

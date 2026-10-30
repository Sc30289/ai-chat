FROM python:3.12-slim

WORKDIR /app

# 只用标准库，不需要 pip install；复制源码即可
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    AI_CHAT_NO_OPEN=1 \
    HOST=0.0.0.0

EXPOSE 8765

# 同时兼容 Render / Railway / Fly 等平台注入的 PORT
CMD ["sh", "-c", "python server.py --host 0.0.0.0 --port ${PORT:-8765} --no-open"]

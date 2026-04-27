FROM python:3.12-alpine

RUN apk add --no-cache python3 py3-pip ffmpeg curl && \
    pip3 install --no-cache-dir --break-system-packages \
        uiprotect==10.3.1 \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.32.0" \
        aiohttp==3.10.10 \
        av==13.1.0

WORKDIR /app

COPY talkback_server.py ./

CMD ["python3", "/app/talkback_server.py"]

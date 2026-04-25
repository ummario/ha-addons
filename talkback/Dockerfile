FROM python:3.12-alpine

RUN apk add --no-cache python3 py3-pip ffmpeg curl && \
    pip3 install --no-cache-dir --break-system-packages \
        uiprotect==10.3.1 \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.32.0" \
        python-multipart==0.0.20

WORKDIR /app
COPY talkback_server.py talk.html ./
CMD ["python3", "/app/talkback_server.py"]

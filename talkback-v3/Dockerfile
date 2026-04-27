FROM python:3.12-alpine

# Build deps necessárias para compilar PyAV (av) na Alpine musl
RUN apk add --no-cache \
        python3 py3-pip ffmpeg curl \
        ffmpeg-dev pkgconfig gcc musl-dev python3-dev \
    && pip3 install --no-cache-dir --break-system-packages \
        uiprotect==10.3.1 \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.32.0" \
    && apk del pkgconfig gcc musl-dev python3-dev ffmpeg-dev \
    && rm -rf /root/.cache /tmp/*

WORKDIR /app

COPY talkback_server.py ./

CMD ["python3", "/app/talkback_server.py"]

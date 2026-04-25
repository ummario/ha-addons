FROM ghcr.io/home-assistant/amd64-base-python:3.12

RUN apk add --no-cache ffmpeg curl bash

WORKDIR /app

RUN pip3 install --no-cache-dir \
    uiprotect==10.3.1 \
    fastapi==0.115.6 \
    "uvicorn[standard]==0.32.0" \
    python-multipart==0.0.20

COPY talkback_server.py talk.html run.sh ./
RUN chmod +x run.sh && mkdir -p /tmp/talkback-chunks

CMD ["/app/run.sh"]

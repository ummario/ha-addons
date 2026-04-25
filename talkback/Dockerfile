ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip ffmpeg curl && \
    pip3 install --no-cache-dir --break-system-packages \
        uiprotect==10.3.1 \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.32.0" \
        python-multipart==0.0.20

WORKDIR /app
COPY talkback_server.py talk.html run.sh ./
RUN chmod +x run.sh && mkdir -p /tmp/talkback-chunks

CMD ["/app/run.sh"]

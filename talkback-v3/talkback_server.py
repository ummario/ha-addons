"""
Talkback Server v3 — STANDALONE add-on (NVR-direct AAC-LC).

Endpoint:
  GET /         — info simples
  GET /health   — diagnóstico
  WS  /ws-v3    — talkback (NVR-direct, AAC-LC, latência alvo <1s)

Lê /data/options.json do add-on para credenciais e parametros AAC.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from typing import Optional

import av
import aiohttp
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from uiprotect import ProtectApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [v3] %(message)s")
log = logging.getLogger("talkback-v3")

# Lê opcoes
try:
    with open("/data/options.json") as _f:
        _opts = json.load(_f)
    for _k, _v in _opts.items():
        os.environ[_k.upper()] = str(_v)
    log.info("Opcoes carregadas: %s", list(_opts.keys()))
except Exception as _e:
    log.warning("options.json nao encontrado: %s", _e)

HOST       = os.environ.get("UFP_ADDRESS", "10.20.30.100")
PORT       = int(os.environ.get("UFP_PORT", "443"))
USER       = os.environ.get("UFP_USERNAME", "")
PASS       = os.environ.get("UFP_PASSWORD", "")
API_KEY    = os.environ.get("UFP_API_KEY", "")
SSL_VERIFY = os.environ.get("UFP_SSL_VERIFY", "false").lower() == "true"
CAMERA_ID  = os.environ.get("UFP_CAMERA_ID", "")
LISTEN_PORT = int(os.environ.get("TALKBACK_V3_PORT", "3007"))

PCM_RATE = 24000
PCM_BITS = 16
PCM_CHANNELS = 1

# Configuráveis via opções do add-on
AAC_RATE = int(os.environ.get("AAC_RATE", "22050"))
AAC_BITRATE = int(os.environ.get("AAC_BITRATE", "24000"))

app = FastAPI(title="Talkback v3 (NVR-direct)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST","OPTIONS"], allow_headers=["*"])

protect_v3: Optional[ProtectApiClient] = None
_last_connect_error: Optional[str] = None
_session_lock = asyncio.Lock()


async def _ensure_connected() -> None:
    global protect_v3, _last_connect_error
    if protect_v3 is not None:
        return
    log.info("A ligar ao NVR %s:%d como %s...", HOST, PORT, USER)
    try:
        protect_v3 = ProtectApiClient(HOST, PORT, USER, PASS, api_key=API_KEY, verify_ssl=SSL_VERIFY)
        await protect_v3.update()
        _last_connect_error = None
        log.info("Ligado. Cameras: %d", len(protect_v3.bootstrap.cameras))
    except Exception as e:
        protect_v3 = None
        _last_connect_error = str(e)
        raise


@app.on_event("startup")
async def startup() -> None:
    log.info("Talkback v3 add-on arrancou (porta %d)", LISTEN_PORT)
    log.info("USER=%s API_KEY=%s CAMERA_ID=%s", USER, (API_KEY[:8]+"...") if API_KEY else "", CAMERA_ID)
    log.info("AAC: rate=%d Hz, bitrate=%d bps", AAC_RATE, AAC_BITRATE)
    missing = [k for k, v in {"USER": USER, "PASS": PASS, "API_KEY": API_KEY, "CAMERA_ID": CAMERA_ID}.items() if not v]
    if missing:
        log.error("Vars em falta: %s", missing)
        return
    try:
        await _ensure_connected()
    except Exception as e:
        log.error("Ligacao inicial falhou: %s", e)


@app.get("/")
async def index() -> JSONResponse:
    return JSONResponse({"service": "talkback-v3", "version": "3.0.0", "endpoints": ["/", "/health", "/ws-v3"]})


@app.get("/health")
async def health() -> JSONResponse:
    if protect_v3 is None:
        try:
            await _ensure_connected()
        except Exception:
            pass
    info = {
        "ok": protect_v3 is not None,
        "version": "3.0.0",
        "service": "talkback-v3-addon",
        "camera_id": CAMERA_ID or None,
        "connected": protect_v3 is not None,
        "transport": "ws-direct-to-nvr",
        "codec": "aac-lc",
        "sample_rate_in": PCM_RATE,
        "sample_rate_out": AAC_RATE,
        "bitrate": AAC_BITRATE,
    }
    if _last_connect_error:
        info["error"] = _last_connect_error
    if protect_v3 is not None and CAMERA_ID:
        try:
            cam = protect_v3.bootstrap.cameras.get(CAMERA_ID)
            if cam:
                info["camera_name"] = cam.name
                info["has_speaker"] = cam.feature_flags.has_speaker
        except Exception as e:
            info["bootstrap_error"] = str(e)
    return JSONResponse(info)


async def _get_nvr_talkback_url(camera_id: str) -> str:
    if protect_v3 is None:
        raise RuntimeError("ProtectApiClient nao inicializado")
    base = f"https://{HOST}:{PORT}"
    path = f"/proxy/protect/api/ws/talkback?camera={camera_id}"
    async with protect_v3._session.get(base + path, ssl=SSL_VERIFY) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"NVR talkback URL HTTP {resp.status}: {body[:200]}")
        data = await resp.json()
        url = data.get("url")
        if not url:
            raise RuntimeError(f"NVR talkback resposta sem url: {data}")
        log.info("NVR devolveu talkback URL")
        return url


class _AacEncoder:
    def __init__(self) -> None:
        self._buf = io.BytesIO()
        self._container = av.open(self._buf, mode="w", format="adts")
        self._stream = self._container.add_stream("aac", rate=AAC_RATE)
        self._stream.bit_rate = AAC_BITRATE
        try:
            self._stream.layout = "mono"
        except Exception:
            pass
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=AAC_RATE)
        self._closed = False

    def encode_pcm(self, pcm_bytes: bytes) -> bytes:
        if self._closed:
            return b""
        n_samples = len(pcm_bytes) // 2
        if n_samples == 0:
            return b""
        frame = av.AudioFrame(format="s16", layout="mono", samples=n_samples)
        frame.sample_rate = PCM_RATE
        frame.planes[0].update(pcm_bytes)
        for resampled in self._resampler.resample(frame):
            for packet in self._stream.encode(resampled):
                self._container.mux(packet)
        return self._drain()

    def _drain(self) -> bytes:
        data = self._buf.getvalue()
        if not data:
            return b""
        self._buf.seek(0)
        self._buf.truncate(0)
        return data

    def close(self) -> bytes:
        if self._closed:
            return b""
        self._closed = True
        try:
            for packet in self._stream.encode(None):
                self._container.mux(packet)
            self._container.close()
        except Exception as e:
            log.warning("erro a fechar encoder: %s", e)
        return self._drain()


@app.websocket("/ws-v3")
async def ws_talkback_v3(ws: WebSocket) -> None:
    await ws.accept()
    log.info("WS aberto: %s", ws.client)
    try:
        await _ensure_connected()
    except Exception as e:
        await ws.send_json({"event": "error", "msg": f"NVR connect failed: {e}"})
        await ws.close()
        return
    assert protect_v3 is not None
    camera = protect_v3.bootstrap.cameras.get(CAMERA_ID)
    if camera is None:
        await ws.send_json({"event": "error", "msg": f"camera {CAMERA_ID} nao encontrada"})
        await ws.close()
        return
    if _session_lock.locked():
        await ws.send_json({"event": "error", "msg": "outra sessao v3 activa"})
        await ws.close()
        return
    async with _session_lock:
        await _run_session(ws, camera)


async def _run_session(ws_browser: WebSocket, camera) -> None:
    t_start = time.monotonic()
    bytes_pcm_in = 0
    bytes_aac_out = 0
    encoder: Optional[_AacEncoder] = None
    nvr_ws: Optional[aiohttp.ClientWebSocketResponse] = None
    try:
        t0 = time.monotonic()
        nvr_url = await _get_nvr_talkback_url(camera.id)
        log.info("NVR URL obtido em %.0fms", (time.monotonic() - t0) * 1000)
        t0 = time.monotonic()
        nvr_ws = await protect_v3._session.ws_connect(nvr_url, ssl=SSL_VERIFY, heartbeat=10)
        log.info("NVR WS aberto em %.0fms", (time.monotonic() - t0) * 1000)
        encoder = _AacEncoder()
        await ws_browser.send_json({"event": "ready", "rate": PCM_RATE, "bits": PCM_BITS, "channels": PCM_CHANNELS, "version": "v3", "codec_out": "aac-lc", "rate_out": AAC_RATE})
        log.info("Pronto a receber PCM, %.0fms total setup", (time.monotonic() - t_start) * 1000)
        last_log_t = time.monotonic()
        while True:
            if nvr_ws.closed:
                log.warning("NVR WS fechou inesperadamente, code=%s", nvr_ws.close_code)
                break
            try:
                msg = await ws_browser.receive()
            except WebSocketDisconnect:
                log.info("WS browser desconectou")
                break
            if msg["type"] == "websocket.disconnect":
                log.info("WS browser disconnect")
                break
            pcm = msg.get("bytes")
            if pcm is None:
                continue
            bytes_pcm_in += len(pcm)
            try:
                aac = encoder.encode_pcm(pcm)
            except Exception as e:
                log.exception("erro a encodar PCM: %s", e)
                break
            if aac:
                bytes_aac_out += len(aac)
                try:
                    await nvr_ws.send_bytes(aac)
                except Exception as e:
                    log.warning("erro a enviar ao NVR WS: %s", e)
                    break
            now = time.monotonic()
            if now - last_log_t > 2.0:
                elapsed = now - t_start
                pcm_kbps = (bytes_pcm_in * 8 / 1000) / elapsed
                aac_kbps = (bytes_aac_out * 8 / 1000) / elapsed
                log.info("PCM in: %.1f kbps (%d bytes) | AAC out: %.1f kbps (%d bytes)", pcm_kbps, bytes_pcm_in, aac_kbps, bytes_aac_out)
                last_log_t = now
        log.info("Sessao terminou: %d PCM, %d AAC, %.1fs", bytes_pcm_in, bytes_aac_out, time.monotonic() - t_start)
    except Exception as e:
        log.exception("erro na sessao: %s", e)
        try:
            await ws_browser.send_json({"event": "error", "msg": str(e)})
        except Exception:
            pass
    finally:
        if encoder is not None:
            try:
                tail = encoder.close()
                if tail and nvr_ws is not None and not nvr_ws.closed:
                    await nvr_ws.send_bytes(tail)
                    log.info("flush final: %d bytes AAC", len(tail))
            except Exception as e:
                log.warning("erro no flush: %s", e)
        if nvr_ws is not None:
            try:
                await nvr_ws.close()
                log.info("NVR WS fechado")
            except Exception:
                pass
        try:
            if ws_browser.client_state.name != "DISCONNECTED":
                await ws_browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT, log_level="info")

"""
Talkback Server v2.0.2 — WebSocket + FIFO streaming com pre-buffer 200ms.

Mudanças relativamente a v2.0.1:
  - PREBUFFER_MS reduzido de 500ms para 200ms para menor perda na 1a palavra.
  - 9600 bytes de PCM em vez de 24000 antes do TalkbackStream arrancar.
  - PyAV ainda consegue probe do WAV header com este buffer mais curto.


Arquitectura:
  Browser AudioWorklet  ──[WebSocket binário, PCM s16le 24kHz mono]──►  /ws
                                                                          │
                                                                          ▼
                                                              ┌─────────────────────┐
                                                              │ Escreve PCM no FIFO │
                                                              │ /tmp/talkback.fifo  │
                                                              └──────────┬──────────┘
                                                                         │
                                                                         ▼
                                                              ┌─────────────────────┐
                                                              │ TalkbackStream lê   │
                                                              │ FIFO via PyAV,      │
                                                              │ encoda OPUS RTP,    │
                                                              │ UDP pacing → câmara │
                                                              └─────────────────────┘

Endpoints:
  GET  /            — serve talk.html
  GET  /health      — diagnóstico
  WS   /ws          — recebe PCM binário do browser, alimenta o stream

Notas:
  - PCM esperado: 16-bit signed little-endian, 24000 Hz, mono.
  - Frame size do AudioWorklet (browser): 128 samples = ~5.3ms a 24kHz.
  - O AudioWorklet faz o downsample de 48k→24k antes de enviar.
  - A sessão UniFi (create_talkback_session_public) abre uma vez por WS,
    fecha quando o WS fecha.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from uiprotect import ProtectApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("talkback")

# Ler opcoes do HA add-on
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
LISTEN_PORT = int(os.environ.get("TALKBACK_PORT", "3006"))
MOCK       = os.environ.get("TALKBACK_MOCK", "0") == "1"

# PCM input format (do browser)
PCM_RATE = 24000
PCM_BITS = 16
PCM_CHANNELS = 1

FIFO_PATH = "/tmp/talkback.fifo"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

protect: Optional[ProtectApiClient] = None
_last_connect_error: Optional[str] = None
_session_lock = asyncio.Lock()  # impede 2 sessões talkback em simultâneo


def _make_wav_header(sample_rate: int = PCM_RATE,
                     bits_per_sample: int = PCM_BITS,
                     channels: int = PCM_CHANNELS) -> bytes:
    """
    Header WAV de 44 bytes para tamanho 'infinito' (0xFFFFFFFF).
    PyAV ignora o size field quando lê de FIFO/stream, mas o header
    é necessário para identificar formato.
    """
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    return (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)  # size placeholder
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", 16)            # fmt chunk size
        + struct.pack("<H", 1)             # PCM format
        + struct.pack("<H", channels)
        + struct.pack("<I", sample_rate)
        + struct.pack("<I", byte_rate)
        + struct.pack("<H", block_align)
        + struct.pack("<H", bits_per_sample)
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)    # data size placeholder
    )


async def _ensure_connected() -> None:
    global protect, _last_connect_error
    if MOCK or protect is not None:
        return
    log.info("A ligar ao NVR %s:%d como %s...", HOST, PORT, USER)
    try:
        protect = ProtectApiClient(HOST, PORT, USER, PASS, api_key=API_KEY, verify_ssl=SSL_VERIFY)
        await protect.update()
        _last_connect_error = None
        log.info("Ligado. Cameras: %d", len(protect.bootstrap.cameras))
    except Exception as e:
        protect = None
        _last_connect_error = str(e)
        raise


@app.on_event("startup")
async def startup() -> None:
    log.info("Talkback v2.0.2 — WS+FIFO streaming (pre-buffer 200ms)")
    log.info("Modo: %s", "MOCK" if MOCK else "PRODUCAO")
    log.info("USER=%s API_KEY=%s CAMERA_ID=%s",
             USER, (API_KEY[:8]+"...") if API_KEY else "", CAMERA_ID)

    # Limpar FIFO se existir
    try:
        if os.path.exists(FIFO_PATH):
            os.unlink(FIFO_PATH)
    except OSError as e:
        log.warning("nao limpou FIFO: %s", e)

    missing = [k for k, v in {"USER": USER, "PASS": PASS, "API_KEY": API_KEY,
                              "CAMERA_ID": CAMERA_ID}.items() if not v]
    if missing and not MOCK:
        log.error("Vars em falta: %s. Preencher opcoes do add-on.", missing)
        return
    if not MOCK:
        try:
            await _ensure_connected()
        except Exception as e:
            log.error("Ligacao inicial falhou: %s", e)


@app.get("/")
async def index() -> FileResponse:
    html_path = Path(__file__).parent / "talk.html"
    if not html_path.exists():
        raise HTTPException(500, "talk.html nao encontrado")
    return FileResponse(html_path)


@app.get("/health")
async def health() -> JSONResponse:
    if not MOCK and protect is None:
        try:
            await _ensure_connected()
        except Exception:
            pass

    info = {
        "ok": MOCK or protect is not None,
        "version": "2.0.2",
        "mock": MOCK,
        "camera_id": CAMERA_ID or None,
        "connected": protect is not None,
        "transport": "websocket+fifo",
    }
    if _last_connect_error:
        info["error"] = _last_connect_error
    if protect is not None and CAMERA_ID:
        try:
            await protect.update()
            cam = protect.bootstrap.cameras.get(CAMERA_ID)
            if cam:
                info["camera_name"] = cam.name
                info["has_speaker"] = cam.feature_flags.has_speaker
        except Exception as e:
            info["bootstrap_error"] = str(e)
    return JSONResponse(info)


@app.websocket("/ws")
async def ws_talkback(ws: WebSocket) -> None:
    """
    Recebe PCM binário do browser, escreve no FIFO, mantém TalkbackStream a ler.

    Protocolo:
      Cliente envia frames PCM s16le 24kHz mono em mensagens binárias.
      Servidor envia mensagens JSON de status: {"event": "...", ...}.
    """
    await ws.accept()
    log.info("WS aberto: %s", ws.client)

    if MOCK:
        await ws.send_json({"event": "ready", "mock": True})
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        log.info("WS fechado (mock)")
        return

    # Verificar conexão NVR
    try:
        await _ensure_connected()
    except Exception as e:
        await ws.send_json({"event": "error", "msg": f"NVR connect failed: {e}"})
        await ws.close()
        return

    assert protect is not None
    camera = protect.bootstrap.cameras.get(CAMERA_ID)
    if camera is None:
        await ws.send_json({"event": "error", "msg": f"camera {CAMERA_ID} nao encontrada"})
        await ws.close()
        return

    # Lock global: só 1 sessão talkback em simultâneo
    if _session_lock.locked():
        await ws.send_json({"event": "error", "msg": "outra sessao talkback activa"})
        await ws.close()
        return

    async with _session_lock:
        await _run_talkback_session(ws, camera)


async def _run_talkback_session(ws: WebSocket, camera) -> None:
    """
    Sessão talkback completa. Estratégia:
      1. Criar FIFO
      2. Abrir FIFO writer (em thread, fica blocked até reader abrir)
      3. Enviar 'ready' ao browser; receber PCM e PRE-BUFFERAR (~500ms)
         enquanto o FIFO writer ainda não está aberto
      4. Quando o pre-buffer encher, criar TalkbackStream + start()
         (isto abre o reader do FIFO, libertando o writer)
      5. Despejar pre-buffer todo no FIFO de uma vez
      6. Continuar a receber PCM do WS e a escrever no FIFO
      7. No fim, fechar writer (EOF), aguardar stream completar, stop()

    O pre-buffer evita o problema de PyAV ficar a tentar fazer probe num
    FIFO com só 44 bytes. Damos-lhe um pacote inicial substancial.
    """
    fifo_path = FIFO_PATH
    PREBUFFER_MS = 200
    PREBUFFER_BYTES = (PCM_RATE * PCM_CHANNELS * PCM_BITS // 8) * PREBUFFER_MS // 1000
    # 24000 * 1 * 2 * 0.2 = 9600 bytes

    # 1. Criar FIFO
    try:
        if os.path.exists(fifo_path):
            os.unlink(fifo_path)
        os.mkfifo(fifo_path)
    except OSError as e:
        await ws.send_json({"event": "error", "msg": f"mkfifo: {e}"})
        await ws.close()
        return

    loop = asyncio.get_running_loop()
    fifo_fd: Optional[int] = None
    stream = None
    bytes_written = 0
    t_start = time.monotonic()
    prebuffer = bytearray(_make_wav_header())  # já começa com header

    try:
        # 2. Anunciar ready ao browser para começar a enviar PCM
        await ws.send_json({"event": "ready", "rate": PCM_RATE,
                             "bits": PCM_BITS, "channels": PCM_CHANNELS})
        log.info("Pre-buffer alvo: %d bytes (%dms)", PREBUFFER_BYTES, PREBUFFER_MS)

        # 3. Acumular pre-buffer
        while len(prebuffer) - len(_make_wav_header()) < PREBUFFER_BYTES:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
            except asyncio.TimeoutError:
                raise RuntimeError("timeout a acumular pre-buffer")
            if msg["type"] == "websocket.disconnect":
                log.info("WS disconnect durante pre-buffer")
                return
            data = msg.get("bytes")
            if data:
                prebuffer.extend(data)
                bytes_written += len(data)

        log.info("Pre-buffer pronto: %d bytes total", len(prebuffer))

        # 4. Criar TalkbackStream e iniciar
        log.info("A criar talkback stream para %s (FIFO=%s)", camera.name, fifo_path)
        stream = await camera.create_talkback_stream(fifo_path)
        await stream.start()
        log.info("Stream iniciado, _error=%r is_running=%s",
                 stream._error, stream.is_running)

        # 5. Abrir FIFO para escrita (agora que reader está aberto)
        def _open_fifo_write():
            return os.open(fifo_path, os.O_WRONLY)

        fifo_fd = await loop.run_in_executor(None, _open_fifo_write)
        log.info("FIFO writer aberto, fd=%d", fifo_fd)

        # 6. Despejar pre-buffer
        await loop.run_in_executor(None, os.write, fifo_fd, bytes(prebuffer))
        log.info("Pre-buffer escrito no FIFO (%d bytes)", len(prebuffer))

        # Verificar logo se o stream já não morreu
        if stream._error:
            raise RuntimeError(f"stream error logo apos prebuffer: {stream._error}")

        # 7. Loop principal: receber PCM do WS, escrever no FIFO
        last_log_t = time.monotonic()
        while True:
            if not stream.is_running:
                log.warning("Stream parou inesperadamente, _error=%r", stream._error)
                break

            try:
                msg = await ws.receive()
            except WebSocketDisconnect:
                log.info("WS desconectou (cliente)")
                break

            if msg["type"] == "websocket.disconnect":
                log.info("WS disconnect message")
                break

            data = msg.get("bytes")
            if data is None:
                continue

            # Escrever PCM no FIFO
            try:
                await loop.run_in_executor(None, os.write, fifo_fd, data)
                bytes_written += len(data)
            except BrokenPipeError:
                log.warning("FIFO broken pipe — stream parou? _error=%r", stream._error)
                break
            except OSError as e:
                log.error("FIFO write error: %s", e)
                break

            # Log periódico de débito
            now = time.monotonic()
            if now - last_log_t > 2.0:
                kbps = (bytes_written * 8 / 1000) / (now - t_start)
                log.info("Throughput: %.1f kbps (%d bytes), stream.is_running=%s",
                         kbps, bytes_written, stream.is_running)
                last_log_t = now

        log.info("Sessão terminou: %d bytes em %.1fs",
                 bytes_written, time.monotonic() - t_start)

    except Exception as e:
        log.exception("Erro na sessão talkback: %s", e)
        try:
            await ws.send_json({"event": "error", "msg": str(e)})
        except Exception:
            pass

    finally:
        # Fechar FIFO writer (sinaliza EOF ao stream)
        if fifo_fd is not None:
            try:
                os.close(fifo_fd)
                log.info("FIFO writer fechado")
            except OSError:
                pass

        # Aguardar stream completar (até 2s) e capturar erro
        if stream is not None:
            try:
                # Dar tempo ao thread interno de processar e flush
                for _ in range(20):
                    if not stream.is_running:
                        break
                    await asyncio.sleep(0.1)
                await stream.stop()
                if stream._error:
                    log.error("TalkbackStream final error: %s", stream._error)
                else:
                    log.info("TalkbackStream parado limpo")
            except Exception as e:
                log.warning("erro a parar stream: %s", e)

        # Apagar FIFO
        try:
            if os.path.exists(fifo_path):
                os.unlink(fifo_path)
        except OSError:
            pass

        try:
            if ws.client_state.name != "DISCONNECTED":
                await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT, log_level="info")

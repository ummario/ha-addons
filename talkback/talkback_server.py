"""
Talkback Server — HA Add-on version
Suporta ingress do Home Assistant.
"""
from __future__ import annotations
import asyncio, logging, os, time, uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from uiprotect import ProtectApiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("talkback")

HOST       = os.environ.get("UFP_ADDRESS", "10.20.30.100")
PORT       = int(os.environ.get("UFP_PORT", "443"))
USER       = os.environ.get("UFP_USERNAME", "")
PASS       = os.environ.get("UFP_PASSWORD", "")
API_KEY    = os.environ.get("UFP_API_KEY", "")
SSL_VERIFY = os.environ.get("UFP_SSL_VERIFY", "false").lower() == "true"
CAMERA_ID  = os.environ.get("UFP_CAMERA_ID", "")
LISTEN_PORT = int(os.environ.get("TALKBACK_PORT", "3006"))
MOCK       = os.environ.get("TALKBACK_MOCK", "0") == "1"

CHUNK_DIR = Path("/tmp/talkback-chunks")
CHUNK_DIR.mkdir(exist_ok=True)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

protect: ProtectApiClient | None = None
_playback_lock = asyncio.Lock()


async def _ensure_connected() -> None:
    global protect
    if MOCK or protect is not None:
        return
    log.info("A ligar ao NVR %s:%d como %s...", HOST, PORT, USER)
    protect = ProtectApiClient(HOST, PORT, USER, PASS, api_key=API_KEY, verify_ssl=SSL_VERIFY)
    await protect.update()
    log.info("Ligado. Cameras: %d", len(protect.bootstrap.cameras))


async def _wake_speaker() -> None:
    if MOCK or protect is None:
        return
    room_id = f"PR-{uuid.uuid4()}"
    try:
        async with protect._session.post(
            f"https://{HOST}:{PORT}/proxy/access/api/v2/device/{CAMERA_ID}/remote_call",
            json={"device_id": CAMERA_ID, "agora_channel": room_id,
                  "mode": "webrtc", "room_id": room_id, "source": "web"},
            headers={"X-API-KEY": API_KEY},
            ssl=SSL_VERIFY,
        ) as resp:
            data = await resp.json()
            log.info("wake_speaker: %s", data.get("codeS", "?"))
    except Exception as e:
        log.warning("wake_speaker falhou: %s", e)


@app.on_event("startup")
async def startup() -> None:
    log.info("Modo: %s", "MOCK" if MOCK else "PRODUCAO")
    if not MOCK and all([USER, PASS, API_KEY, CAMERA_ID]):
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


@app.post("/push")
async def push(request: Request) -> JSONResponse:
    body = await request.body()
    if not body:
        raise HTTPException(400, "body vazio")
    chunk_id = uuid.uuid4().hex[:8]
    chunk_path = CHUNK_DIR / f"chunk_{chunk_id}.webm"
    chunk_path.write_bytes(body)
    log.info("Chunk %s (%.1f KB)", chunk_id, len(body) / 1024)
    if MOCK:
        chunk_path.unlink(missing_ok=True)
        return JSONResponse({"ok": True, "mock": True})
    try:
        await _ensure_connected()
        assert protect is not None
        camera = protect.bootstrap.cameras.get(CAMERA_ID)
        if camera is None:
            raise HTTPException(404, f"camera {CAMERA_ID} nao encontrada")
        async with _playback_lock:
            t0 = time.monotonic()
            await _wake_speaker()
            await asyncio.sleep(0.8)
            await camera.play_audio(str(chunk_path), blocking=True)
            log.info("Chunk %s em %.2fs", chunk_id, time.monotonic() - t0)
        return JSONResponse({"ok": True, "bytes": len(body)})
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Falha: %s", e)
        raise HTTPException(500, str(e))
    finally:
        chunk_path.unlink(missing_ok=True)


@app.get("/health")
async def health() -> JSONResponse:
    info = {
        "ok": True,
        "mock": MOCK,
        "camera_id": CAMERA_ID or None,
        "connected": protect is not None,
    }
    if protect is not None and CAMERA_ID:
        cam = protect.bootstrap.cameras.get(CAMERA_ID)
        if cam:
            info["camera_name"] = cam.name
            info["has_speaker"] = cam.feature_flags.has_speaker
    return JSONResponse(info)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT, log_level="info")

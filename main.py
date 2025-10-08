import os
import json
import asyncio
import aiohttp
import httpx
import time
from datetime import timedelta
import logging
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
from supabase import create_client, Client
from livekit import rtc
from livekit.api import AccessToken, VideoGrants

# ──────────────────────── CONFIG ────────────────────────
load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
log = logging.getLogger("anna-agent")

def _env(name: str, required: bool = True) -> str:
    val = os.getenv(name)
    if not val and required:
        raise RuntimeError(f"Missing required env var: {name}")
    return val

CARTESIA_API_KEY = _env("CARTESIA_API_KEY")
LIVEKIT_URL = _env("LIVEKIT_URL")
LIVEKIT_API_KEY = _env("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = _env("LIVEKIT_API_SECRET")
SUPABASE_URL = _env("SUPABASE_URL")
SUPABASE_KEY = _env("SUPABASE_KEY")
WEBHOOK_SECRET = _env("WEBHOOK_SECRET")
GROK_API_KEY = _env("GROK_API_KEY")
GROK_API_URL = _env("GROK_API_URL")
log.info(f"[Config] All environment variables loaded.")

# ──────────────────────── SUPABASE ────────────────────────
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info("[Supabase] ✅ Connected")
except Exception as e:
    supabase = None
    log.error(f"[Supabase] ❌ Connection failed: {e}. Check your SUPABASE_URL and SUPABASE_KEY.")

# ──────────────────────── FASTAPI ────────────────────────
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    log.info("[System] 🚀 Anna-Agent initialized (Cartesia + LiveKit + Supabase + Grok).")

@app.get("/ping")
async def ping():
    return {"status": "ok", "cartesia": bool(CARTESIA_API_KEY), "livekit": bool(LIVEKIT_API_KEY), "grok": bool(GROK_API_KEY)}

# ──────────────────────── HELPERS ────────────────────────
SAMPLE_RATE = 44100
NUM_CHANNELS = 1

def _verify_webhook(request: Request):
    if request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

def _safe_log_event(event: str, status: str):
    if not supabase:
        log.warning("[Supabase] ⚠️ Skipped logging (no connection)")
        return
    try:
        supabase.table("memories").insert({"user_id": event, "reply": status}).execute()
        log.info(f"[Supabase] ✅ Logged event: {status} for {event}")
    except Exception as e:
        log.error(f"[Supabase] ❌ Logging failed: {e}")

# ──────────────────────── LIVEKIT + CARTESIA ────────────────────────
def _build_livekit_join_token(identity: str, room: str) -> str:
    grants = VideoGrants(
        room_join=True,
        room_create=True,
        room_admin=True,
        can_publish=True,
        can_subscribe=True,
        room=room
    )
    
    token = AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity(identity) \
        .with_grants(grants) \
        .with_ttl(timedelta(seconds=3600)) \
        .to_jwt()

    log.info(f"[LiveKit] 🎫 Token successfully generated for {identity}")
    return token

async def connect_livekit_room(identity="anna", room_name="anna"):
    source = rtc.AudioSource(sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track("cartesia_pcm", source)
    room = rtc.Room()

    @room.on("participant_connected")
    def on_participant_connected(p): log.info(f"[LiveKit] 👥 {p.identity} joined")

    jwt_token = _build_livekit_join_token(identity, room_name)
    log.info("[LiveKit] 🔗 Connecting to room…")

    try:
        await room.connect(LIVEKIT_URL, jwt_token)
        await room.local_participant.publish_track(track)
        log.info("[LiveKit] ✅ Connected & published audio track.")
    except Exception as e:
        log.error(f"[LiveKit] ❌ RTC connection failed: {str(e)}")
        raise
    return room, source

async def send_tts_stream(text: str, pcm_sink: rtc.AudioSource):
    async with httpx.AsyncClient(timeout=30.0) as hc:
        headers = {
            "Authorization": f"Bearer {CARTESIA_API_KEY}",
            "Cartesia-Version": "2024-03-15"
        }
        r = await hc.post(
            "https://api.cartesia.ai/v1/tts/credentials",
            headers=headers,
        )
        r.raise_for_status()
        token = r.json()["token"]
    log.info("[Cartesia] 🎫 Short-lived token acquired.")

    ws_url = "wss://api.cartesia.ai/v1/audio/stream"
    start_msg = {
        "type": "start",
        "output": {"format": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE, "container": "raw"},
        "voice": {"mode": "id", "id": "9c7dc287-1354-4fcc-a706-377f9a44e238"},
        "input": {"type": "text", "text": text},
    }

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            ws_url,
            headers={"Authorization": f"Bearer {token}"},
            heartbeat=30.0,
            autoping=True,
            timeout=90.0,
        ) as ws:
            log.info("[Cartesia] 🔊 WebSocket TTS start")
            await ws.send_str(json.dumps(start_msg))

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    pcm_bytes = msg.data
                    if pcm_bytes:
                        frame = rtc.AudioFrame.from_pcm(
                            pcm_bytes, sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS
                        )
                        pcm_sink.capture_frame(frame)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    if "end" in msg.data:
                        log.info("[Cartesia] ✅ Stream ended")
                        break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"Cartesia WebSocket error: {ws.exception()}")

    log.info("[Cartesia] ✅ Stream complete.")

# ──────────────────────── GROK ────────────────────────
async def push_to_grok(session_id, transcript):
    async with httpx.AsyncClient() as client:
        payload = {
            "messages": [{"role": "user", "content": transcript}],
            "model": "grok-4-fast-reasoning"
        }
        headers = {"Authorization": f"Bearer {GROK_API_KEY}"}
        response = await client.post(GROK_API_URL, json=payload, headers=headers)
        response.raise_for_status()
        log.info(f"[Grok] ✅ Analytics pushed for session {session_id}")

# ──────────────────────── HANDLER ────────────────────────
@app.post("/handle_convo")
async def handle_convo(payload: dict, request: Request):
    _verify_webhook(request)
    log.info(f"[Webhook] Incoming: {payload}")
    event_type = payload.get("type")
    req_id = payload.get("request_id", "unknown")

    if event_type == "call_started":
        log.info(f"[Webhook] Handling event 'call_started' for {req_id}")
        _safe_log_event(req_id, "call_started")
        log.info("[Anna] ▶ Greeting: 'Alo?'")

        room = None
        try:
            room, source = await connect_livekit_room(identity="anna", room_name="anna")
            await send_tts_stream("Alo?", source)
        except Exception as e:
            log.error(f"[LiveKit] ❌ Connection failed: {e}")
            raise
        finally:
            if room:
                try:
                    await room.disconnect()
                    log.info("[LiveKit] 📴 Disconnected cleanly.")
                except Exception as e:
                    log.error(f"[LiveKit] ⚠️ Error during disconnect: {e}")
        return {"status": "accepted"}

    elif event_type == "call_completed":
        log.info(f"[Webhook] Handling event 'call_completed' for {req_id}")
        _safe_log_event(req_id, "call_completed")
        await push_to_grok(req_id, "Mock transcript")
        return {"status": "completed"}

    return {"status": "ignored"}

# ──────────────────────── ENTRY ────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

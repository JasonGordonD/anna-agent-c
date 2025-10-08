import os
import json
import asyncio
from fastapi import FastAPI, Request
from supabase import create_client, Client
from livekit import api, rtc
import httpx

# ─────────────────────────────────────────────
#  Environment Configuration
# ─────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY")
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2025-04-16")

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "anna-webhook-secret-2025")

# ─────────────────────────────────────────────
#  Supabase Client
# ─────────────────────────────────────────────
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ─────────────────────────────────────────────
#  FastAPI Application
# ─────────────────────────────────────────────
app = FastAPI()

# ─────────────────────────────────────────────
#  LiveKit Token Generator
# ─────────────────────────────────────────────
def get_livekit_token(identity="anna", room="anna"):
    grant = api.VideoGrant(room_join=True, room=room)
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_grants(grant)
        .with_identity(identity)
        .to_jwt()
    )
    return token

# ─────────────────────────────────────────────
#  Cartesia Short-lived Access Token
# ─────────────────────────────────────────────
async def get_cartesia_token():
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.cartesia.ai/access-token",
            headers={
                "Authorization": f"Bearer {CARTESIA_API_KEY}",
                "Cartesia-Version": CARTESIA_VERSION,
                "Content-Type": "application/json",
            },
            json={"grants": {"tts": True, "stt": True}, "expires_in": 60},
        )
        data = resp.json()
        return data.get("token")

# ─────────────────────────────────────────────
#  LiveKit Connection
# ─────────────────────────────────────────────
async def connect_livekit_room():
    token = get_livekit_token()
    room = rtc.Room()
    await room.connect(LIVEKIT_URL, token)
    print("[LiveKit] 🎧 Connected to room anna")

    # Subscribe to Cartesia audio
    @room.on("track_subscribed")
    async def on_track(track, publication, participant):
        if track.kind == "audio":
            print("[LiveKit] 🔊 Remote Cartesia audio active")
            await rtc.play_audio(track)

    # Publish microphone track
    mic = await rtc.create_local_audio_track("mic")
    await room.local_participant.publish_track(mic)
    print("[LiveKit] 🎤 Microphone published")

    await asyncio.Future()  # keep alive

# ─────────────────────────────────────────────
#  Webhook Handler
# ─────────────────────────────────────────────
@app.post("/handle_convo")
async def handle_convo(req: Request):
    payload = await req.json()
    event_type = payload.get("type")
    request_id = payload.get("request_id")
    print(f"[Webhook] Incoming: {json.dumps(payload)}")

    if event_type == "call_started":
        print(f"[Webhook] Handling event 'call_started' for {request_id}")
        asyncio.create_task(connect_livekit_room())

        # Log event in Supabase
        supabase.table("memories").insert({
            "user_id": request_id,
            "reply": "Event logged: call_started",
            "schema_vars": json.dumps({"event": "call_started"}),
        }).execute()

        print("[Anna] ▶ Greeting: 'Alo?'")
        return {"status": "accepted"}

    elif event_type == "call_completed":
        print(f"[Webhook] Handling event 'call_completed' for {request_id}")
        supabase.table("memories").insert({
            "user_id": request_id,
            "reply": "Event logged: call_completed",
            "schema_vars": json.dumps({"event": "call_completed"}),
        }).execute()
        print("[Anna] 🧩 Finalizing session", request_id)
        return {"status": "completed"}

    else:
        print(f"[Webhook] Unhandled event type: {event_type}")
        return {"status": "ignored"}

# ─────────────────────────────────────────────
#  Health Endpoints
# ─────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "alive", "service": "Anna-Agent-C"}

@app.get("/status")
async def status():
    return {"ok": True, "status": "running"}

# ─────────────────────────────────────────────
#  Local Runner
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

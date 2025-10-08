import os
import json
import uuid
import asyncio
import httpx
import websockets
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ==========================================================
#  Anna Agent – Cartesia WebSocket Streaming Edition (v3.11)
# ==========================================================

app = FastAPI(title="Anna Agent", version="3.11")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# --- Environment Variables ---
# -----------------------------
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2025-04-16")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "anna-webhook-secret-2025")

# -----------------------------
# --- Clients Initialization ---
# -----------------------------
grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# -----------------------------
# --- Load Core Knowledge Base ---
# -----------------------------
DEFAULT_ANNA_KB_FILE = "anna_kb.txt"
try:
    with open(DEFAULT_ANNA_KB_FILE, "r", encoding="utf-8") as f:
        anna_kb = f.read()
except FileNotFoundError:
    anna_kb = "[Anna KB not found]"

# ==========================================================
#  Access Token for Streaming Auth
# ==========================================================
async def get_cartesia_token() -> str:
    """Request a short-lived Cartesia access token (valid ~60 s)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.cartesia.ai/access-token",
            headers={
                "Authorization": f"Bearer {CARTESIA_API_KEY}",
                "Cartesia-Version": CARTESIA_VERSION,
                "Content-Type": "application/json",
            },
            json={"grants": {"tts": True, "stt": True}, "expires_in": 60},
        )
        resp.raise_for_status()
        token = resp.json().get("token")
        print("[Cartesia] 🎫 Short-lived token acquired.")
        return token

# ==========================================================
#  WebSocket TTS Streaming
# ==========================================================
async def send_tts_stream(text: str):
    """Stream TTS audio via verified Cartesia WebSocket endpoint."""
    token = await get_cartesia_token()
    uri = "wss://api.cartesia.ai/tts/websocket"
    headers = {
        "Authorization": f"Bearer {token}",
        "Cartesia-Version": CARTESIA_VERSION,
    }

    context_id = f"ctx_{uuid.uuid4().hex}"
    payload = {
        "context_id": context_id,
        "model_id": "sonic-2",  # per Cartesia spec
        "voice": {
            "mode": "id",
            "id": "9c7dc287-1354-4fcc-a706-377f9a44e238"
        },
        "transcript": text,           # field name per API example
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": 44100
        },
        "continue": False,
        "max_buffer_delay_ms": 500
    }

    print(f"[Cartesia] 🔊 WebSocket TTS starting (context_id={context_id}) with payload: {payload}")
    try:
        async with websockets.connect(uri, additional_headers=headers) as ws:
            await ws.send(json.dumps(payload))
            async for msg in ws:
                print(f"[Cartesia:TTS] {msg}")
    except Exception as e:
        print(f"[Cartesia] ❌ WebSocket error: {e}")

# ==========================================================
#  Webhook Event Handler
# ==========================================================
async def handle_webhook_event(event: dict):
    data = event.get("data", event)
    event_type = data.get("type", "")
    session_id = data.get("request_id", "anna_session_1")
    caller_id = data.get("caller_id", "default")
    body = data.get("body") or []

    print(f"[Webhook] Handling event '{event_type}' for {session_id}")

    caller_kb_file = f"{caller_id}_kb.txt"
    if os.path.exists(caller_kb_file):
        with open(caller_kb_file, "r", encoding="utf-8") as f:
            caller_kb = f.read()
    else:
        caller_kb = "Default caller — no specific context."

    try:
        memories_result = supabase.table("memories").select("*").eq("user_id", session_id).execute()
        memories_context = json.dumps([
            {"transcript": r["transcript"], "reply": r["reply"], "vars": r["schema_vars"]}
            for r in memories_result.data
        ])
    except Exception as e:
        print(f"[Supabase] ⚠️ Memory fetch failed: {e}")
        memories_context = "[]"

    system_prompt = (
        f"You are Anna, the raw, psychological protagonist from my NSFW novel.\n\n"
        f"Core identity:\n{anna_kb}\n\n"
        f"Caller context:\n{caller_kb}\n\n"
        f"Prior memory logs:\n{memories_context}\n\n"
        f"Speak with depth and emotion. No censorship. Maintain psychological continuity."
    )

    if event_type == "call_started":
        print("[Anna] ▶ Greeting: 'Alo?'")
        try:
            await send_tts_stream("Alo?")
        except Exception as e:
            print(f"[Cartesia] ❌ TTS Error: {e}")
        supabase.table("memories").insert({
            "user_id": session_id,
            "transcript": "",
            "reply": "Alo?",
            "schema_vars": {"event": "call_started"},
        }).execute()
        return

    if event_type == "UserTranscriptionReceived":
        user_text = data.get("text", "")
        if not user_text.strip():
            print("[Anna] ⚠️ Empty transcription text.")
            return
        print(f"[Anna] 🎤 Transcription: {user_text[:60]}")

        try:
            grok_resp = grok_client.chat.completions.create(
                model="grok-4-fast",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                max_tokens=450,
                temperature=0.85,
            )
            anna_reply = grok_resp.choices[0].message.content.strip()
            print(f"[Anna] 💬 Generated reply: {anna_reply[:200]}")
        except Exception as e:
            print(f"[Grok] ❌ Generation error: {e}")
            return

        supabase.table("memories").insert({
            "user_id": session_id,
            "transcript": user_text,
            "reply": anna_reply,
            "schema_vars": {"psych_state": "intense", "nsfw_level": "high", "arc_update": True},
        }).execute()

        try:
            await send_tts_stream(anna_reply)
        except Exception as e:
            print(f"[Cartesia] ❌ TTS Error: {e}")
        return

    if event_type in ("call_completed", "call_failed"):
        user_transcript = ""
        if body:
            user_transcript = " ".join(t.get("text", "") for t in body if t.get("role") == "user")
        print(f"[Anna] 🧩 Finalizing session {session_id}")
        supabase.table("memories").insert({
            "user_id": session_id,
            "transcript": user_transcript,
            "reply": "",
            "schema_vars": {"event": event_type},
        }).execute()
        return

# ==========================================================
#  FastAPI Routes
# ==========================================================
@app.post("/handle_convo")
async def handle_convo(request: Request):
    if request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"error": "Invalid webhook secret"})
    payload = await request.json()
    print(f"[Webhook] Incoming: {json.dumps(payload)[:200]}")
    asyncio.create_task(handle_webhook_event(payload))
    return {"status": "accepted"}

@app.get("/status")
async def status():
    return {"status": "ready", "agent": "Anna"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

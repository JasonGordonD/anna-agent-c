import json
import os
import asyncio
import httpx
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ==========================================================
#  Anna Agent – Cartesia-Managed LiveKit Edition (v2.2)
#  Verified against Cartesia API docs (2025-04-16)
# ==========================================================

app = FastAPI(title="Anna Agent", version="2.2")

# -----------------------------
# --- Middleware & Config ---
# -----------------------------
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
#  Helper: Cartesia TTS – aligned with documented Bytes API
# ==========================================================
async def send_tts(text: str) -> bool:
    """
    Generate speech via Cartesia TTS (Bytes endpoint per docs).
    Falls back to minimal legacy path if regional routing differs.
    """
    endpoints = [
        "https://api.cartesia.ai/v1/audio/tts/bytes",  # Primary per API reference
        "https://api.cartesia.ai/v1/tts/bytes",       # Legacy fallback
    ]
    headers = {
        "Authorization": f"Bearer {CARTESIA_API_KEY}",
        "Cartesia-Version": CARTESIA_VERSION,
        "Accept": "audio/wav",
        "Content-Type": "application/json",
    }
    payload = {
        "model_id": "sonic-fast",
        "voice_id": "default",
        "text": text,
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 44100,
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        for url in endpoints:
            try:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code == 200 and len(response.content) > 50:
                    print(f"[Cartesia] ✅ TTS success via {url}: '{text[:50]}'")
                    return True
                else:
                    print(f"[Cartesia] ⚠️ {url} → {response.status_code}: {response.text[:120]}")
            except Exception as e:
                print(f"[Cartesia] ❌ Exception contacting {url}: {e}")

        print("[Cartesia] ❌ All documented endpoints failed.")
        print("[Cartesia] ℹ️ If your account uses real-time streaming, check /v1/tts/stream or /v1/tts/websocket in Cartesia docs.")
        return False

# ==========================================================
#  Webhook Event Handler
# ==========================================================
async def handle_webhook_event(event: dict):
    """Central processor for Cartesia webhooks."""
    data = event.get("data", event)
    event_type = data.get("type", "")
    session_id = data.get("request_id", "anna_session_1")
    caller_id = data.get("caller_id", "default")
    body = data.get("body") or []  # guarantee iterable

    # ---- Load caller KB ----
    caller_kb_file = f"{caller_id}_kb.txt"
    try:
        with open(caller_kb_file, "r", encoding="utf-8") as f:
            caller_kb = f.read()
    except FileNotFoundError:
        caller_kb = "Default caller—no specific context."

    print(f"[Anna] Loaded KB for {caller_id} ({len(caller_kb)} chars)")

    # ---- Retrieve prior memory ----
    try:
        memories_result = supabase.table("memories").select("*").eq("user_id", session_id).execute()
        memories_context = json.dumps(
            [
                {
                    "transcript": r["transcript"],
                    "reply": r["reply"],
                    "vars": r["schema_vars"],
                }
                for r in memories_result.data
            ]
        )
    except Exception as e:
        print(f"[Supabase] ⚠️ Failed to fetch memory: {e}")
        memories_context = "[]"

    system_prompt = (
        f"You are Anna, the raw, psychological protagonist from my NSFW novel.\n\n"
        f"Core identity (static):\n{anna_kb}\n\n"
        f"Caller context (optional):\n{caller_kb}\n\n"
        f"Prior memory logs:\n{memories_context}\n\n"
        f"Speak with deep emotion. Update Anna's psyche after every reply. No emotional censorship."
    )

    # ======================================================
    #  Event-specific logic
    # ======================================================
    if event_type == "call_started":
        print("[Anna] ▶ Call started — greeting 'Alo?'")
        await send_tts("Alo?")
        supabase.table("memories").insert(
            {"user_id": session_id, "transcript": "", "reply": "Alo?", "schema_vars": {"event": "call_started"}}
        ).execute()
        return

    if event_type in ("call_completed", "call_failed"):
        user_transcript = ""
        if body:
            user_transcript = " ".join([turn.get("text", "") for turn in body if turn.get("role") == "user"])
        print(f"[Anna] 🧩 Processing {event_type} for {session_id}")
        supabase.table("memories").insert(
            {"user_id": session_id, "transcript": user_transcript, "reply": "", "schema_vars": {"event": event_type}}
        ).execute()

        if not user_transcript.strip():
            print("[Anna] No user transcript → skipping reply generation.")
            return

        grok_response = grok_client.chat.completions.create(
            model="grok-4-fast",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_transcript}],
            max_tokens=450,
            temperature=0.85,
        )
        anna_reply = grok_response.choices[0].message.content.strip()
        schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}
        supabase.table("memories").insert(
            {"user_id": session_id, "transcript": user_transcript, "reply": anna_reply, "schema_vars": schema_vars}
        ).execute()
        print(f"[Anna] ✅ Stored memory: {anna_reply[:100]}")
        return

    if event_type == "UserTranscriptionReceived":
        user_transcript = data.get("text", "")
        if not user_transcript.strip():
            print("[Anna] Empty transcription text — skipping.")
            return
        print(f"[Anna] 🎤 Transcription: {user_transcript[:60]}...")
        grok_response = grok_client.chat.completions.create(
            model="grok-4-fast",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_transcript}],
            max_tokens=450,
            temperature=0.85,
        )
        anna_reply = grok_response.choices[0].message.content.strip()
        schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}
        supabase.table("memories").insert(
            {"user_id": session_id, "transcript": user_transcript, "reply": anna_reply, "schema_vars": schema_vars}
        ).execute()
        print(f"[Anna] 💬 Reply: {anna_reply[:100]}")
        await send_tts(anna_reply)
        return

# ==========================================================
#  FastAPI Routes
# ==========================================================
@app.get("/")
async def root():
    return {"status": "online", "agent": "Anna", "endpoint": "/handle_convo"}

@app.get("/status")
async def status():
    return {"status": "ready", "agent": "Anna"}

@app.post("/connect")
async def connect_agent(request: Request):
    data = await request.json()
    caller_id = data.get("caller_id", "default")
    return {"agent_name": "Anna", "caller_id": caller_id, "status": "connected"}

@app.post("/handle_convo")
async def handle_convo(request: Request):
    """Primary webhook endpoint for Cartesia-managed agent."""
    if request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"error": "Invalid webhook secret"})
    try:
        payload = await request.json()
        print(f"[Webhook] Incoming: {json.dumps(payload)[:300]}...")
        asyncio.create_task(handle_webhook_event(payload))
        return {"status": "accepted"}
    except Exception as e:
        print(f"[Webhook] ❌ {e}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# ==========================================================
#  Application Entry
# ==========================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

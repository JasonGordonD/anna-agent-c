import json
import os
import asyncio
import httpx
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

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

# Environment Variables (Render)
GROK_API_KEY = os.getenv("GROK_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2025-04-16")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "anna-webhook-secret-2025")

# Initialize clients
grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# -----------------------------
# --- Knowledge Base Load ---
# -----------------------------
DEFAULT_ANNA_KB_FILE = "anna_kb.txt"
if os.path.exists(DEFAULT_ANNA_KB_FILE):
    with open(DEFAULT_ANNA_KB_FILE, "r", encoding="utf-8") as f:
        anna_kb = f.read()
else:
    anna_kb = "[Anna KB not found]"

# -----------------------------
# --- Helper: Cartesia TTS ---
# -----------------------------
async def send_tts(text: str):
    """Send a TTS request to Cartesia-managed agent."""
    url = "https://api.cartesia.ai/v1/audio/tts"
    headers = {
        "Authorization": f"Bearer {CARTESIA_API_KEY}",
        "Cartesia-Version": CARTESIA_VERSION,
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
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code == 200 and len(response.content) > 50:
                print(f"[Cartesia] TTS played successfully: '{text[:50]}'")
                return True
            else:
                print(f"[Cartesia] TTS failed {response.status_code}: {response.text[:100]}")
                return False
    except Exception as e:
        print(f"[Cartesia] TTS error: {str(e)}")
        return False

# -----------------------------
# --- Webhook Handler ---
# -----------------------------
async def handle_webhook_event(event):
    """Handles Cartesia webhook events."""
    data = event.get("data", event)
    event_type = data.get("type", "")
    session_id = data.get("request_id", "anna_session_1")
    caller_id = data.get("caller_id", "default")
    body = data.get("body", [])

    # Load KB (Anna + caller)
    caller_kb_file = f"{caller_id}_kb.txt"
    if os.path.exists(caller_kb_file):
        with open(caller_kb_file, "r", encoding="utf-8") as f:
            caller_kb = f.read()
    else:
        caller_kb = "Default caller—no specific context."

    print(f"[Anna] Loaded KB for {caller_id} ({len(caller_kb)} chars)")

    # Fetch prior memory
    memories_result = supabase.table("memories").select("*").eq("user_id", session_id).execute()
    memories_context = json.dumps(
        [{"transcript": row["transcript"], "reply": row["reply"], "vars": row["schema_vars"]}
         for row in memories_result.data]
    )

    system_prompt = (
        f"You are Anna, the raw, psychological protagonist from my NSFW novel.\n\n"
        f"Core identity (static):\n{anna_kb}\n\n"
        f"Caller context (optional):\n{caller_kb}\n\n"
        f"Prior memory logs:\n{memories_context}\n\n"
        f"Speak with deep emotion. Update Anna's psyche after every reply. No emotional censorship."
    )

    # Event branching
    if event_type == "call_started":
        print("[Anna] Call started → sending greeting 'Alo?'")
        await send_tts("Alo?")
        return

    if event_type in ["call_completed", "call_failed"]:
        user_transcript = " ".join([turn["text"] for turn in body if turn.get("role") == "user"])
        if not user_transcript.strip():
            print("[Anna] No user transcript → skipping memory insert.")
            return
        print(f"[Anna] Processing call_completed event for session {session_id}")

        grok_response = grok_client.chat.completions.create(
            model="grok-4-fast",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_transcript}],
            max_tokens=450,
            temperature=0.85,
        )
        anna_reply = grok_response.choices[0].message.content.strip()
        schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}
        update_data = {
            "user_id": session_id,
            "transcript": user_transcript,
            "reply": anna_reply,
            "schema_vars": schema_vars,
        }
        supabase.table("memories").insert(update_data).execute()
        print(f"[Anna] Stored final memory: {anna_reply[:100]}...")
        return

    if event_type == "UserTranscriptionReceived":
        user_transcript = data.get("text", "")
        if not user_transcript.strip():
            return
        print(f"[Anna] New transcription: {user_transcript[:60]}...")

        grok_response = grok_client.chat.completions.create(
            model="grok-4-fast",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_transcript}],
            max_tokens=450,
            temperature=0.85,
        )
        anna_reply = grok_response.choices[0].message.content.strip()
        schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}

        supabase.table("memories").insert(
            {"user_id": session_id, "transcript": user_transcript,
             "reply": anna_reply, "schema_vars": schema_vars}
        ).execute()
        print(f"[Anna] Generated live reply: {anna_reply[:100]}...")
        await send_tts(anna_reply)
        return


# -----------------------------
# --- FastAPI Routes ---
# -----------------------------
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
        print(f"[Webhook] Received: {json.dumps(payload)[:300]}...")
        asyncio.create_task(handle_webhook_event(payload))
        return {"status": "accepted"}
    except Exception as e:
        print(f"[Webhook] Error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})


# -----------------------------
# --- Startup for Render ---
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

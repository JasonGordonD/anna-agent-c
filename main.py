import json
import os
import asyncio
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import cartesia

app = FastAPI()

# Allow CORS for Cartesia's frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Use env vars (set in Render/Cartesia)
GROK_API_KEY = os.getenv("GROK_API_KEY", "fallback_placeholder")
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://qumhcrbukjhfwcsoxpyr.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InF1bWhjcmJ1a2poZndjc294cHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTk1ODE5MjIsImV4cCI6MjA3NTE1NzkyMn0.EYOMJ7kEZ3uvkIqcJhDVS3PCrlHx2JrkFTP6OuVg3PI")
WEBHOOK_SECRET = "anna-webhook-secret-2025"
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")  # Add to .env post-canon
AGENT_ID = "agent_gSYqjM538vcm2tq6yaAnNX"  # From logs

# Clients
grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
cartesia_client = cartesia.Cartesia(api_key=CARTESIA_API_KEY)

# Load Anna's default KB at startup
DEFAULT_ANNA_KB_FILE = "anna_kb.txt"
if os.path.exists(DEFAULT_ANNA_KB_FILE):
    with open(DEFAULT_ANNA_KB_FILE, "r", encoding="utf-8") as f:
        anna_kb = f.read()
else:
    anna_kb = "[Anna KB not found]"

# Initialize Line Agent (runs on agent startup, separate process ideal)
async def start_agent():
    agent = cartesia.LineAgent(api_key=CARTESIA_API_KEY, agent_id=AGENT_ID)
    await agent.connect()
    await agent.send_initial_message("Alo?")  # Initial TTS trigger
    # Webhook callback (syncs with /handle_convo)
    async def on_webhook(event):
        await handle_webhook_event(event)
    agent.on("webhook", on_webhook)
    await agent.run()  # Keeps agent alive

async def handle_webhook_event(event):
    data = event.data
    event_type = data.get("type", "")
    session_id = data.get("request_id", "anna_session_1")
    caller_id = data.get("caller_id", "default")
    body = data.get("body", [])

    caller_kb_file = f"{caller_id}_kb.txt"
    if os.path.exists(caller_kb_file):
        with open(caller_kb_file, "r", encoding="utf-8") as f:
            caller_kb = f.read()
    else:
        caller_kb = "Default caller—no specific context."
    print(f"Loaded KB for {caller_id}: {len(caller_kb)} chars")

    memories_result = supabase.table("memories").select("*").eq("user_id", session_id).execute()
    memories_context = json.dumps([{"transcript": row["transcript"], "reply": row["reply"], "vars": row["schema_vars"]} for row in memories_result.data])
    system_prompt = (
        f"You are Anna, the raw, psychological protagonist from my NSFW novel.\n\n"
        f"Core identity (static):\n{anna_kb}\n\n"
        f"Caller context (optional):\n{caller_kb}\n\n"
        f"Prior memory logs:\n{memories_context}\n\n"
        f"Speak with deep emotion. Update Anna's psyche after every reply. No emotional censorship."
    )

    if event_type == "call_started":
        # Initial message sent via agent.connect
        pass
    elif event_type == "call_completed" or event_type == "call_failed":
        user_transcript = " ".join([turn["text"] for turn in body if turn.get("role") == "user"])
        if user_transcript.strip():
            grok_response = grok_client.chat.completions.create(
                model="grok-4-fast",
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_transcript}],
                max_tokens=450,
                temperature=0.85
            )
            anna_reply = grok_response.choices[0].message.content.strip()
            schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}
            update_data = {"user_id": session_id, "transcript": user_transcript, "reply": anna_reply, "schema_vars": schema_vars}
            supabase.table("memories").insert(update_data).execute()
            print(f"Reply generated and stored for completed: {anna_reply[:100]}...")
        else:
            print("No user transcript—skipping insert")
    elif event_type == "UserTranscriptionReceived":
        user_transcript = data.get("text", "")
        if user_transcript.strip():
            grok_response = grok_client.chat.completions.create(
                model="grok-4-fast",
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_transcript}],
                max_tokens=450,
                temperature=0.85
            )
            anna_reply = grok_response.choices[0].message.content.strip()
            schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}
            update_data = {"user_id": session_id, "transcript": user_transcript, "reply": anna_reply, "schema_vars": schema_vars}
            supabase.table("memories").insert(update_data).execute()
            print(f"Reply generated for transcription: {anna_reply[:100]}...")
            await agent.send_message(anna_reply)  # Real-time reply via SDK

@app.get("/")
async def root():
    return {"status": "online", "agent": "Anna", "endpoint": "/handle_convo"}

@app.get("/status")
async def status():
    return {"status": "ready", "agent": "Anna"}

@app.post("/connect")
async def connect_agent(request: Request):
    try:
        data = await request.json()
    except:
        data = {}
    caller_id = data.get("caller_id", "default")
    return {"agent_name": "Anna", "caller_id": caller_id, "status": "connected"}

@app.post("/handle_convo")
async def handle_convo(request: Request):
    if request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        return JSONResponse(status_code=403, content={"error": "Invalid webhook secret"})
    try:
        print(f"Cartesia call received - Headers: {dict(request.headers)}")
        data = await request.json()
        print(f"Payload: {data}")
        asyncio.create_task(handle_webhook_event(data))  # Async delegate to agent callback
        return {"status": "accepted"}  # Acknowledge webhook
    except Exception as e:
        print(f"Error in handle_convo: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    import asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(start_agent())  # Run agent in background
    uvicorn.run(app, host="0.0.0.0", port=8000)

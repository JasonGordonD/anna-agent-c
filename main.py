import json
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI()

# Allow CORS for Cartesia's frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hardcoded keys (replace with env vars for production)
GROK_API_KEY = "xai-Ek68wWERkwgdeMbYbyXRR5l499OjkMhrULZb8720R1Cn4NG4tKofGtaOKnQgA0VduXv34NHsTqr5v7vg"
SUPABASE_URL = "https://qumhcrbukjhfwcsoxpyr.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InF1bWhjcmJ1a2poZndjc294cHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTk1ODE5MjIsImV4cCI6MjA3NTE1NzkyMn0.EYOMJ7kEZ3uvkIqcJhDVS3PCrlHx2JrkFTP6OuVg3PI"

# Clients
grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# Load Anna's default KB at startup
DEFAULT_ANNA_KB_FILE = "anna_kb.txt"
if os.path.exists(DEFAULT_ANNA_KB_FILE):
    with open(DEFAULT_ANNA_KB_FILE, "r", encoding="utf-8") as f:
        anna_kb = f.read()
else:
    anna_kb = "[Anna KB not found]"

@app.get("/")
async def root():
    return {"status": "online", "agent": "Anna", "endpoint": "/handle_convo"}

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
    try:
        data = await request.json()
        user_transcript = data.get("transcript", "")
        session_id = data.get("session_id", "anna_session_1")
        caller_id = data.get("caller_id", "default")

        # Load caller KB dynamically based on caller_id
        caller_kb_file = f"{caller_id}_kb.txt"
        if os.path.exists(caller_kb_file):
            with open(caller_kb_file, "r", encoding="utf-8") as f:
                caller_kb = f.read()
        else:
            caller_kb = "Default caller—no specific context."
        print(f"Loaded KB for {caller_id}: {len(caller_kb)} chars")  # Debug log

        # Retrieve session memories from Supabase
        memories_result = supabase.table("memories").select("*").eq("user_id", session_id).execute()
        memories_context = json.dumps([
            {
                "transcript": row["transcript"],
                "reply": row["reply"],
                "vars": row["schema_vars"]
            }
            for row in memories_result.data
        ])

        # Combine static and dynamic KBs into system prompt
        system_prompt = (
            f"You are Anna, the raw, psychological protagonist from my NSFW novel.\n\n"
            f"Core identity (static):\n{anna_kb}\n\n"
            f"Caller context (optional):\n{caller_kb}\n\n"
            f"Prior memory logs:\n{memories_context}\n\n"
            f"Speak with deep emotion. Update Anna's psyche after every reply. No emotional censorship."
        )

        # Generate Grok reply
        grok_response = grok_client.chat.completions.create(
            model="grok-4-fast",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_transcript}
            ],
            max_tokens=450,
            temperature=0.85
        )
        anna_reply = grok_response.choices[0].message.content.strip()

        # Store new memory to Supabase
        schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}
        update_data = {
            "user_id": session_id,
            "transcript": user_transcript,
            "reply": anna_reply,
            "schema_vars": schema_vars
        }
        supabase.table("memories").insert(update_data).execute()

        return {"audio": "stream_success", "reply_text": anna_reply, "status": "success"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

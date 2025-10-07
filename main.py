import json
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

# Hardcoded for testing (your actual keys)
GROK_API_KEY = "xai-Ek68wWERkwgdeMbYbyXRR5l499OjkMhrULZb8720R1Cn4NG4tKofGtaOKnQgA0VduXv34NHsTqr5v7vg"
SUPABASE_URL = "https://qumhcrbukjhfwcsoxpyr.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InF1bWhjcmJ1a2poZndjc294cHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTk1ODE5MjIsImV4cCI6MjA3NTE1NzkyMn0.EYOMJ7kEZ3uvkIqcJhDVS3PCrlHx2JrkFTP6OuVg3PI"

# Clients
grok_client = OpenAI(api_key=GROK_API_KEY, base_url="https://api.x.ai/v1")
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

@app.post("/handle_convo")
async def handle_convo(request: Request):
    try:
        data = await request.json()
        user_transcript = data.get("transcript", "")
        session_id = data.get("session_id", "anna_session_1")

        # Fetch memories (ignore if table empty)
        memories_result = supabase.table("memories").select("*").eq("user_id", session_id).execute()
        memories_context = json.dumps([{"transcript": row["transcript"], "reply": row["reply"], "vars": row["schema_vars"]} for row in memories_result.data])

        # Grok generates reply
        grok_response = grok_client.chat.completions.create(
            model="grok-4-fast",
            messages=[
                {
                    "role": "system",
                    "content": f"You are Anna, the raw, psychological protagonist from my NSFW novel: Backstory: Scarred by a forbidden affair that shattered her trust, Anna navigates desire and trauma with unfiltered vulnerability—craving intimacy while fearing its cost. Speak with biting emotion, dive into NSFW candor where it fits her arc. Use memories: {memories_context}. Evolve her psyche based on context—no holding back."
                },
                {"role": "user", "content": user_transcript}
            ],
            max_tokens=450,
            temperature=0.85
        )
        anna_reply = grok_response.choices[0].message.content.strip()

        # Update Supabase
        schema_vars = {"psych_state": "intense", "nsfw_level": "high", "arc_update": True}
        update_data = {
            "user_id": session_id,
            "transcript": user_transcript,
            "reply": anna_reply,
            "schema_vars": schema_vars
        }
        supabase.table("memories").insert(update_data).execute()

        # Mock Cartesia TTS for local testing (platform handles real in Cartesia)
        return {"audio": "stream_success", "reply_text": anna_reply, "status": "success"}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

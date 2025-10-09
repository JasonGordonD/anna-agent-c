import aiohttp
import asyncio
import json
import base64

# ───────────────────────── CONFIG ─────────────────────────
API_KEY = "sk_car_7cPWaHSNsfkZSQ6eYBZwkc"
VOICE_ID = "9c7dc287-1354-4fcc-a706-377f9a44e238"  # Your chosen voice
MODEL_ID = "sonic-2"

# ───────────────────────── MAIN ─────────────────────────
async def main():
    url = f"wss://api.cartesia.ai/tts/websocket?api_key={API_KEY}&cartesia_version=2025-04-16"
    total_bytes = 0
    chunk_count = 0

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            # Full phrase ensures a longer audio response
            request = {
                "context_id": "test1",
                "model_id": MODEL_ID,
                "transcript": "Hello world, this is a longer voice test from Cartesia to verify the full audio output.",
                "voice": {"mode": "id", "id": VOICE_ID},
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": 44100
                }
            }

            await ws.send_str(json.dumps(request))
            print("[Cartesia] Request sent. Waiting for audio...")

            with open("hello_clean.pcm", "wb") as f:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        if payload.get("type") == "chunk":
                            data_b64 = payload.get("data")
                            if data_b64:
                                chunk = base64.b64decode(data_b64)
                                f.write(chunk)
                                total_bytes += len(chunk)
                                chunk_count += 1
                        if payload.get("type") == "error":
                            print("[Cartesia] Error:", payload)
                        if payload.get("done") is True:
                            print(f"[Cartesia] Stream complete. "
                                  f"Received {chunk_count} chunks, {total_bytes} bytes.")
                            break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        print("[Cartesia] WebSocket error:", ws.exception())
                        break

if __name__ == "__main__":
    asyncio.run(main())

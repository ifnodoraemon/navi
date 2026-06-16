import asyncio
import os
import httpx

async def main():
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key: return
    
    payload = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "You are a helpful planner. Output JSON."},
            {"role": "user", "content": "Plan a hello world."}
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 65536,
        "temperature": 0
    }
    
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://api.deepseek.com/chat/completions", json=payload, headers=headers)
        print("Status:", resp.status_code)
        print("Response:", resp.text)

asyncio.run(main())

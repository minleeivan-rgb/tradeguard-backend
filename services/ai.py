import os
import httpx

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

async def call_claude(prompt: str, max_tokens: int = 1500) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]
            }
        )
    data = resp.json()
    if resp.status_code != 200:
        raise Exception(f"Claude API 錯誤 {resp.status_code}: {data}")
    return data.get("content", [{}])[0].get("text", "AI 無回應")

import os
import httpx

LINE_API = "https://api.line.me/v2/bot/message/push"
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")

async def send_line_message(text: str):
    """傳送 LINE 訊息給指定 User ID"""
    if not LINE_TOKEN or not LINE_USER_ID:
        print("[LINE] 未設定 token 或 user_id，跳過通知")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                LINE_API,
                headers={
                    "Authorization": f"Bearer {LINE_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": LINE_USER_ID,
                    "messages": [{"type": "text", "text": text}]
                }
            )
        if r.status_code == 200:
            print(f"[LINE] 通知發送成功")
        else:
            print(f"[LINE] 發送失敗 {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[LINE] 發送例外：{e}")

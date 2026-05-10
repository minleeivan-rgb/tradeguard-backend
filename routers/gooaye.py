from fastapi import APIRouter, HTTPException
from datetime import datetime
import httpx
import json
import re
from database import db
from services.ai import call_claude

router = APIRouter(prefix="/gooaye", tags=["gooaye"])

def fix_id(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

def strip_html(html: str) -> str:
    """簡單去除 HTML 標籤，保留文字內容"""
    # 移除 script / style 區塊
    html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL)
    # 移除 HTML 標籤
    text = re.sub(r'<[^>]+>', '\n', html)
    # 清理多餘空白行
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return '\n'.join(lines)

async def fetch_episode_content(ep_num: int) -> str | None:
    """從 socialworkerdaily.com 抓取指定集數的筆記內容"""
    url = f"https://socialworkerdaily.com/notes-of-gooaye-ep-{ep_num}/"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        html = r.text
        # 找 article 或 entry-content 區塊
        match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
        if not match:
            match = re.search(r'class=["\']entry-content["\'][^>]*>(.*?)</div>', html, re.DOTALL)
        if match:
            return strip_html(match.group(1))
        # fallback：直接清整頁 HTML
        return strip_html(html)[:6000]
    except Exception as e:
        print(f"[Gooaye] fetch error EP{ep_num}: {e}")
        return None

async def find_latest_episode() -> int:
    """從 DB 最後一集往上找最新集數"""
    last_doc = await db.gooaye.find_one({}, sort=[("ep", -1)])
    start_ep = (last_doc["ep"] if last_doc else 655)

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        latest = start_ep
        for ep in range(start_ep, start_ep + 20):
            url = f"https://socialworkerdaily.com/notes-of-gooaye-ep-{ep}/"
            try:
                r = await client.head(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    latest = ep
                elif r.status_code == 404:
                    break
            except:
                break
    return latest

@router.post("/fetch")
async def fetch_and_analyze():
    """手動觸發：抓最新集數並用 AI 分析，存入 DB"""
    # 找最新集數
    ep_num = await find_latest_episode()

    # 已分析過就直接回傳
    existing = await db.gooaye.find_one({"ep": ep_num})
    if existing:
        return {**fix_id(existing), "cached": True}

    # 抓內容
    content = await fetch_episode_content(ep_num)
    if not content or len(content) < 100:
        raise HTTPException(status_code=404, detail=f"找不到 EP{ep_num} 的內容，可能尚未更新")

    # Claude 分析
    prompt = f"""以下是股癌 Podcast EP{ep_num} 的筆記內容。
請找出所有明確提到的股票標的，整理成結構化資料。

筆記內容：
{content[:7000]}

請只回傳 JSON，格式如下（不要有 markdown 的 ``` 或其他文字）：
{{
  "ep": {ep_num},
  "summary": "本集重點摘要，2-3句話概括本集主題",
  "stocks": [
    {{
      "ticker": "股票代號（台股用數字如2330，美股用英文如NVDA）",
      "name": "股票中文或英文名稱",
      "market": "tw 或 us",
      "story": "這支股票在本集被提到的背景與故事，2-4句話",
      "sentiment": "bullish 或 bearish 或 neutral",
      "sentiment_reason": "看多/看空/中性的核心理由，一句話"
    }}
  ]
}}

注意：
- 只列出明確提到代號或名稱的股票
- 如果不確定是看多還是看空就填 neutral
- ticker 只填代號，不要帶名字"""

    try:
        raw = await call_claude(prompt, max_tokens=3000)
        raw = raw.strip().lstrip('```json').lstrip('```').rstrip('```').strip()
        result = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 分析失敗：{str(e)}")

    # 存 DB
    doc = {
        "ep": ep_num,
        "summary": result.get("summary", ""),
        "stocks": result.get("stocks", []),
        "source_url": f"https://socialworkerdaily.com/notes-of-gooaye-ep-{ep_num}/",
        "analyzed_at": datetime.utcnow().isoformat(),
    }
    await db.gooaye.insert_one(doc)
    return {**fix_id(doc), "cached": False}

@router.get("/history")
async def get_history():
    """取得所有分析過的集數清單"""
    results = []
    async for doc in db.gooaye.find({}).sort("ep", -1).limit(30):
        results.append(fix_id(doc))
    return results

@router.get("/{ep_num}")
async def get_episode(ep_num: int):
    """取得特定集數的分析結果"""
    doc = await db.gooaye.find_one({"ep": ep_num})
    if not doc:
        raise HTTPException(status_code=404, detail=f"找不到 EP{ep_num}，請先抓取")
    return fix_id(doc)

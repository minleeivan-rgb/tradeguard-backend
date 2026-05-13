from fastapi import APIRouter, HTTPException
from datetime import datetime, date, timedelta
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
    html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '\n', html)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return '\n'.join(lines)

def get_latest_ep_by_calendar() -> int:
    """
    純用週曆算目前最新集數，完全不需要連網。
    基準：EP660 = 2026-05-09（週六）
    規律：每週三(weekday=2)、週六(weekday=5) 各出一集
    """
    ref_ep   = 660
    ref_date = date(2026, 5, 9)
    today    = date.today()
    if today <= ref_date:
        return ref_ep
    count = 0
    d = ref_date + timedelta(days=1)
    while d <= today:
        if d.weekday() in (2, 5):
            count += 1
        d += timedelta(days=1)
    return ref_ep + count

async def fetch_episode_content(ep_num: int) -> str | None:
    """從 socialworkerdaily 抓指定集數內容"""
    url = f"https://socialworkerdaily.com/notes-of-gooaye-ep-{ep_num}/"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
        if r.status_code != 200:
            return None
        html = r.text
        match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL)
        if not match:
            match = re.search(r'class=["\']entry-content["\'][^>]*>(.*?)</div>', html, re.DOTALL)
        if match:
            return strip_html(match.group(1))
        return strip_html(html)[:6000]
    except Exception as e:
        print(f"[Gooaye] fetch error EP{ep_num}: {e}")
        return None

@router.post("/fetch")
async def fetch_and_analyze(ep: int = None):
    """
    手動觸發：抓取並分析最新集數。
    ep: 可選，手動指定集數；不填則用週曆自動算。
    """
    # 決定目標集數
    if ep:
        ep_num = ep
    else:
        ep_num = get_latest_ep_by_calendar()

    # 已分析過直接回傳
    existing = await db.gooaye.find_one({"ep": ep_num})
    if existing:
        return {**fix_id(existing), "cached": True}

    # 抓內容（直接抓目標集數，不掃描）
    content = await fetch_episode_content(ep_num)
    if not content or len(content) < 100:
        raise HTTPException(
            status_code=404,
            detail=f"EP{ep_num} 在 socialworkerdaily 尚未更新，請等幾小時後再試"
        )

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

注意：只列出明確提到代號或名稱的股票，ticker 只填代號不帶名字。"""

    try:
        raw = await call_claude(prompt, max_tokens=3000)
        raw = raw.strip().lstrip('```json').lstrip('```').rstrip('```').strip()
        result = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 分析失敗：{str(e)}")

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
    results = []
    async for doc in db.gooaye.find({}).sort("ep", -1).limit(30):
        results.append(fix_id(doc))
    return results

@router.get("/{ep_num}")
async def get_episode(ep_num: int):
    doc = await db.gooaye.find_one({"ep": ep_num})
    if not doc:
        raise HTTPException(status_code=404, detail=f"找不到 EP{ep_num}，請先抓取")
    return fix_id(doc)

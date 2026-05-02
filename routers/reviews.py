from fastapi import APIRouter, HTTPException
from datetime import datetime
from database import db
from models import Review, AIReviewRequest
from services.yfinance_service import calculate_technical_indicators, is_tw_trading_hours, get_tw_realtime_price
from services.ai import call_claude

router = APIRouter(prefix="/reviews", tags=["reviews"])

def fix_id(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

@router.post("/ai")
async def ai_review(req: AIReviewRequest):
    # 技術分析（台股用 FinMind，美股用 yfinance）
    if req.market == "tw":
        from services.finmind import get_tw_technical, get_tw_stock_price
        tech = await get_tw_technical(req.ticker)
        # 盤中用即時價格
        if tech and is_tw_trading_hours():
            realtime = await get_tw_realtime_price(req.ticker)
            if realtime:
                tech["current_price"] = realtime["current_price"]
                tech["price_source"] = "即時"
            else:
                tech["price_source"] = "收盤價"
        elif tech:
            tech["price_source"] = "收盤價"
    else:
        tech = calculate_technical_indicators(req.ticker, req.market)
        if tech:
            tech["price_source"] = "yfinance"

    custom_rules = []
    async for r in db.custom_rules.find({"user_id": req.user_id}):
        custom_rules.append(f"【{r['title']}】{r['content']}")
    rules_text = "\n".join(custom_rules) if custom_rules else "（尚無自訂規則）"

    if req.type == "buy":
        inputs = req.inputs
        tech_summary = ""
        if tech:
            tech_summary = f"""
技術面資料（系統自動抓取）：
- 現價：{tech['current_price']}
- RSI：{tech['rsi']}
- KD：K={tech['kd']['k']} D={tech['kd']['d']}，{'黃金交叉' if tech['kd']['golden_cross'] else '死亡交叉' if tech['kd']['death_cross'] else '無交叉'}
- MACD：{'多頭' if tech['macd']['bullish'] else '空頭'}，{'柱狀體擴張' if tech['macd']['expanding'] else '柱狀體收縮'}
- 均線：月線{'+' if tech['ma20_diff_pct']>0 else ''}{tech['ma20_diff_pct']}%，季線{'+' if tech['ma60_diff_pct']>0 else ''}{tech['ma60_diff_pct']}%
- 布林通道位置：{tech['bollinger']['pct']}%（0=下緣,100=上緣）
- 成交量：{tech['volume']['ratio']}x 均量
- 多方信號：{', '.join(tech['bull_signals']) or '無'}
- 空方信號：{', '.join(tech['bear_signals']) or '無'}
- 技術面方向：{tech['direction']}
"""
        checked_rules = inputs.get('checked_rules', [])
        rules_by_cat = {'entry':[], 'tw_entry':[], 'us_entry':[], 'stoploss':[], 'takeprofit':[], 'mindset':[]}
        for r in checked_rules:
            cat = r.get('category', 'entry')
            rules_by_cat.setdefault(cat, []).append(f"【{r['title']}】{r['content']}")

        entry_cats = {'entry', 'tw_entry', 'us_entry'}
        entry_rules = []
        advisory_rules = []
        for cat, rlist in rules_by_cat.items():
            if cat in entry_cats:
                entry_rules.extend(rlist)
            else:
                advisory_rules.extend(rlist)

        entry_section = "\n".join(f"  - {r}" for r in entry_rules) if entry_rules else "（未勾選進場規則）"
        advisory_section = "\n".join(f"  - {r}" for r in advisory_rules) if advisory_rules else "（無）"

        prompt = f"""你是一個嚴格的交易紀律夥伴，同時也是有豐富經驗的技術分析師。

股票：{req.ticker}（{req.market.upper()}）
進場理由：{inputs.get('story','（未填）')}
停損計畫：{inputs.get('stop','（未填）')}
目標與退場：{inputs.get('target','（未填）')}

{tech_summary}

━━━ 用戶這次的進場規則（必須全部達標，否則直接不通過）━━━
{entry_section}

━━━ 用戶的背景規則（AI 參考用，不作為否決條件）━━━
{advisory_section}

請以繁體中文，按以下架構回覆：

【進場規則核查】
針對每條進場規則逐一判定：
✅ 符合 或 ❌ 不符合（一句話說明）
核查結論：X/{len(entry_rules)} 條通過

【AI 技術觀察】
- 日線動能：
- 月線/季線：
- KD/RSI：
- MACD：
- 成交量：
- 停損合理性：（評估用戶設定的停損是否合理）
- 目標合理性：（評估用戶設定的目標是否合理）
- 你可能沒注意到的風險：
- ⚠ 與進場規則的潛在衝突：

【最終裁決】
規則：有任何進場規則 ❌ → [不通過]
     進場規則全 ✅ 但 AI 有重大疑慮 → [審慎評估]
     進場規則全 ✅ 且 AI 觀察正面 → [通過]

結論：[不通過] / [審慎評估] / [通過]
一句話說明。語氣直接像嚴格的交易導師。"""
    else:
        prompt = f"""你是一個嚴格的交易紀律夥伴。用戶想賣出 {req.ticker}，賣出理由如下：

「{req.inputs.get('reason','（未填）')}」

用戶的規則：停利=高點回檔{req.profit_pct}%，停損=虧損{req.stoploss_pct}%或跌破月線/季線

用戶的自訂進階規則：
{rules_text}

請以繁體中文回覆：

【理由分析】
分析這個賣出理由是否具體且符合規則。

【最終建議】
✓ 可以賣出 / ⏸ 需要再確認 / ✕ 不應該賣出
一句話說明理由。語氣嚴格直接。"""

    try:
        text = await call_claude(prompt)
        verdict = "fail" if "[不通過]" in text else \
                  "pass" if "[通過]" in text else "hold"

        # 加上分析時間和股價標頭
        from datetime import timezone, timedelta
        tw_tz = timezone(timedelta(hours=8))
        now_tw = datetime.now(tw_tz)
        now_str = now_tw.strftime("%Y-%m-%d %H:%M")
        price_source = tech.get("price_source", "") if tech else ""
        price_str = f"${tech['current_price']}（{price_source}）" if tech else "無法取得"
        header = f"📊 分析時間：{now_str}（台灣時間）　｜　股價：{price_str}\n{'─'*40}\n\n"
        text_with_header = header + text
        await db.reviews.insert_one({
            "user_id": req.user_id, "type": req.type, "ticker": req.ticker,
            "inputs": req.inputs, "ai_response": text_with_header, "verdict": verdict,
            "created_at": datetime.utcnow().isoformat()
        })
        return {"text": text_with_header, "verdict": verdict}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 審核失敗：{str(e)}")

@router.post("")
async def save_review(review: Review):
    data = review.dict()
    data["created_at"] = datetime.utcnow().isoformat()
    result = await db.reviews.insert_one(data)
    return {"id": str(result.inserted_id)}

@router.get("/{user_id}")
async def get_reviews(user_id: str):
    reviews = []
    async for r in db.reviews.find({"user_id": user_id}).sort("created_at", -1).limit(50):
        reviews.append(fix_id(r))
    return reviews

from fastapi import APIRouter, HTTPException
from datetime import datetime
from database import db
from models import Review, AIReviewRequest
from services.yfinance_service import calculate_technical_indicators
from services.ai import call_claude

router = APIRouter(prefix="/reviews", tags=["reviews"])

def fix_id(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

@router.post("/ai")
async def ai_review(req: AIReviewRequest):
    tech = calculate_technical_indicators(req.ticker, req.market)

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
        rules_by_cat = {'entry':[], 'stoploss':[], 'takeprofit':[], 'mindset':[]}
        for r in checked_rules:
            cat = r.get('category', 'entry')
            rules_by_cat.setdefault(cat, []).append(f"【{r['title']}】{r['content']}")

        cat_names = {'entry':'進場條件', 'stoploss':'停損條件', 'takeprofit':'停利條件', 'mindset':'心理與紀律'}
        rules_section = ""
        for cat, rlist in rules_by_cat.items():
            if rlist:
                rules_section += f"\n{cat_names[cat]}：\n" + "\n".join(f"  - {r}" for r in rlist) + "\n"

        if not rules_section:
            rules_section = "（用戶這次沒有勾選任何規則）"

        prompt = f"""你是一個嚴格的交易紀律夥伴，同時也是有豐富經驗的技術分析師。

股票：{req.ticker}（{req.market.upper()}）
進場故事/題材：{inputs.get('story','（未填）')}
技術面補充：{inputs.get('tech','（未填）')}
停損計畫：{inputs.get('stop','（未填）')}
目標與退場條件：{inputs.get('target','（未填）')}

{tech_summary}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用戶這次勾選要遵守的規則：
{rules_section}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

請以繁體中文，嚴格按照以下架構回覆：

【第一部分：規則核查】
針對用戶勾選的每條規則，逐一判定：
格式：✅ 符合 或 ❌ 不符合（附一句說明）

{"（沒有勾選規則，跳過此部分）" if not checked_rules else ""}

規則核查結論：X/{len(checked_rules)} 條通過

【第二部分：AI 技術觀察與衝突偵測】
- 日線動能：
- 月線/季線：
- KD/RSI：
- MACD：
- 成交量：
- 你可能沒注意到的風險：
- ⚠ 與你規則的衝突點：（若 AI 觀察到用戶規則雖然表面達標，但技術面有矛盾的地方，在這裡列出）

【最終裁決】
裁決邏輯（一票否決制）：
- 有任何規則 ❌ → 強制 [不通過]
- 規則全 ✅ 但 AI 發現明顯衝突 → [審慎評估]（列出衝突）
- 規則全 ✅ 且 AI 觀察正面 → [通過]
- 沒有勾選規則 → 純技術面判斷

結論：[不通過] / [審慎評估] / [通過]
一句話說明理由。語氣直接，像嚴格但真心幫助的交易導師。"""
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
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        price_str = f"${tech['current_price']}" if tech else "無法取得"
        header = f"📊 分析時間：{now_str}　｜　分析當下股價：{price_str}\n{'─'*40}\n\n"
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

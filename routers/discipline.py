"""
紀律追蹤：記錄每次警示當下的價格，之後回頭比對「有處理 vs 沒處理」的實際差異

collection: discipline_log
  _id        = {user_id}_{ticker}_{type}_{date}   （同日同類型只記一次）
  user_id, ticker, name, alert_type, message, severity
  alert_date, alert_price
  action     = "pending" | "acted" | "ignored"
  action_note, action_at
  followup   = {d5: {...}, d10: {...}, d20: {...}}  由 /discipline/update 回填
"""
from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from database import db

router = APIRouter(prefix="/discipline", tags=["discipline"])
TW_TZ = timezone(timedelta(hours=8))

TRACK_TYPES = {"loss_alert", "profit_alert", "ma_alert"}
HORIZONS = [5, 10, 20]


class ActionIn(BaseModel):
    action: str = "acted"          # acted | ignored
    note: str = ""


def _today() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


async def log_alerts(user_id: str, alerts: list, prices: dict = None):
    """由 /scan/alerts 呼叫，把當下警示與價格存檔"""
    prices = prices or {}
    d = _today()
    n = 0
    for a in alerts:
        if a.get("type") not in TRACK_TYPES:
            continue
        ticker = a.get("ticker")
        px = prices.get(ticker) or a.get("current_price")
        if not px:
            continue
        _id = f'{user_id}_{ticker}_{a.get("type")}_{d}'
        existing = await db.discipline_log.find_one({"_id": _id})
        if existing:
            continue
        await db.discipline_log.insert_one({
            "_id": _id, "user_id": user_id, "ticker": ticker,
            "name": a.get("name", ticker), "alert_type": a.get("type"),
            "message": a.get("message", ""), "severity": a.get("severity", ""),
            "alert_date": d, "alert_price": float(px),
            "action": "pending", "action_note": "", "action_at": None,
            "followup": {},
            "created_at": datetime.now(TW_TZ).isoformat(),
        })
        n += 1
    return n


@router.get("/{user_id}")
async def list_log(user_id: str, days: int = 60):
    since = (datetime.now(TW_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    docs = [d async for d in db.discipline_log.find(
        {"user_id": user_id, "alert_date": {"$gte": since}})]
    docs.sort(key=lambda x: x["alert_date"], reverse=True)

    acted   = [d for d in docs if d["action"] == "acted"]
    ignored = [d for d in docs if d["action"] == "ignored"]
    pending = [d for d in docs if d["action"] == "pending"]

    def _avg(rows, horizon):
        vals = [r["followup"][f"d{horizon}"]["change_pct"]
                for r in rows
                if r.get("followup", {}).get(f"d{horizon}", {}).get("change_pct") is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    stats = {}
    for hz in HORIZONS:
        stats[f"d{hz}"] = {
            "acted_avg_change_pct": _avg(acted, hz),
            "ignored_avg_change_pct": _avg(ignored, hz),
            "acted_n": sum(1 for r in acted if r.get("followup", {}).get(f"d{hz}")),
            "ignored_n": sum(1 for r in ignored if r.get("followup", {}).get(f"d{hz}")),
        }

    insight = None
    ig10 = stats.get("d10", {}).get("ignored_avg_change_pct")
    if ig10 is not None and stats["d10"]["ignored_n"] >= 3:
        if ig10 < -3:
            insight = (f"未處理的警示，10 個交易日後平均再跌 {abs(ig10)}%"
                       f"（{stats['d10']['ignored_n']} 筆樣本）— 你的規則有在保護你，"
                       f"問題在執行不在規則")
        elif ig10 > 3:
            insight = (f"未處理的警示，10 個交易日後平均反彈 {ig10}%"
                       f"（{stats['d10']['ignored_n']} 筆樣本）— 樣本顯示此類警示可能過於敏感，"
                       f"值得檢討觸發條件")

    return {
        "summary": {
            "total": len(docs), "acted": len(acted),
            "ignored": len(ignored), "pending": len(pending),
            "follow_rate_pct": round(len(acted) / (len(acted) + len(ignored)) * 100, 1)
                               if (acted or ignored) else None,
        },
        "stats": stats, "insight": insight,
        "items": [{k: v for k, v in d.items() if k != "_id"} | {"id": d["_id"]}
                  for d in docs[:80]],
        "note": "樣本數少於 5 筆時統計僅供參考，勿據以推翻既有規則",
    }


@router.post("/{log_id}/action")
async def set_action(log_id: str, body: ActionIn):
    act = body.action if body.action in ("acted", "ignored", "pending") else "acted"
    r = await db.discipline_log.update_one(
        {"_id": log_id},
        {"$set": {"action": act, "action_note": body.note,
                  "action_at": datetime.now(TW_TZ).isoformat()}})
    return {"ok": r.matched_count > 0, "action": act}


@router.get("/update/{user_id}")
async def update_followups(user_id: str):
    """回填後續 5/10/20 個交易日的價格變化（資料取自 market_daily 快照）"""
    docs = [d async for d in db.discipline_log.find({"user_id": user_id})]
    if not docs:
        return {"updated": 0, "note": "無紀錄"}

    all_dates = sorted(await db.market_daily.distinct("date"))
    if len(all_dates) < 5:
        return {"updated": 0, "note": "market_daily 歷史不足，請先執行 /signals/backfill"}

    updated = 0
    for doc in docs:
        ad = doc["alert_date"]
        after = [d for d in all_dates if d > ad]
        fu = dict(doc.get("followup") or {})
        changed = False
        for hz in HORIZONS:
            key = f"d{hz}"
            if key in fu or len(after) < hz:
                continue
            target = after[hz - 1]
            px = await db.market_daily.find_one({"_id": f'{target}_{doc["ticker"]}'})
            if not px:
                continue
            close = px["close"]
            base  = doc["alert_price"]
            fu[key] = {"date": target, "close": close,
                       "change_pct": round((close - base) / base * 100, 2) if base else None}
            changed = True
        if changed:
            await db.discipline_log.update_one({"_id": doc["_id"]},
                                               {"$set": {"followup": fu}})
            updated += 1
    return {"updated": updated, "total": len(docs)}

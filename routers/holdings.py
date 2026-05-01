from fastapi import APIRouter, HTTPException
from bson import ObjectId
from datetime import datetime
from database import db
from models import Holding, HoldingUpdate, NoteUpdate
from services.yfinance_service import get_stock_data, calculate_status

router = APIRouter(prefix="/holdings", tags=["holdings"])

def fix_id(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

LEVERAGED_KEYWORDS = ["2X","2x","3X","3x","QLD","SSO","UPRO","TQQQ","SOXL","TECL","SPXL","UDOW",
                      "00631L","00633L","00670L","00675L","00688L","006203"]

@router.get("/{user_id}")
async def get_holdings(user_id: str, refresh_prices: bool = False):
    holdings = []
    user  = await db.users.find_one({"name": user_id})
    rules = user.get("rules", {}) if user else {}

    async for h in db.holdings.find({"user_id": user_id}):
        h = fix_id(h)

        if refresh_prices:
            live = get_stock_data(h["ticker"], h["market"])
            if not live and h["market"] == "tw":
                # yfinance 抓不到時用 TWSE 每日資料補
                from services.twse import _twse_cache
                perf = _twse_cache.get("performance", {})
                if h["ticker"] in perf:
                    p = perf[h["ticker"]]
                    live = {"current_price": p["current_price"], "highest_price": p["current_price"],
                            "ma20": None, "ma60": None, "ma20_diff_pct": None, "ma60_diff_pct": None}
            if live:
                current = live["current_price"]
                highest = max(live["highest_price"], h.get("highest_price", 0))
                status  = calculate_status(current, h["entry_price"], highest, rules)
                await db.holdings.update_one(
                    {"_id": ObjectId(h["_id"])},
                    {"$set": {"current_price": current, "highest_price": highest,
                              "ma20": live["ma20"], "ma60": live["ma60"],
                              "ma20_diff_pct": live["ma20_diff_pct"],
                              "ma60_diff_pct": live["ma60_diff_pct"],
                              "status": status, "updated_at": datetime.utcnow().isoformat()}}
                )
                h.update({"current_price": current, "highest_price": highest, "status": status, **live})

        if h.get("current_price") and h.get("entry_price"):
            h["pnl_pct"] = round((h["current_price"] - h["entry_price"]) / h["entry_price"] * 100, 2)
        if h.get("highest_price") and h.get("current_price"):
            h["pullback_pct"] = round((h["highest_price"] - h["current_price"]) / h["highest_price"] * 100, 2)

        unit = h.get("unit", "share")
        unit_multiplier  = 1000 if unit == "lot" else 1
        shares_real      = h.get("shares", 0) * unit_multiplier
        current_px       = h.get("current_price") or h.get("entry_price", 0)
        h["market_value"]   = round(current_px * shares_real, 0)
        h["cost"]           = round(h["entry_price"] * shares_real, 0)
        h["shares_display"] = f"{h.get('shares', 0)}{'張' if unit == 'lot' else '股'}"

        if h.get("margin"):
            ratio = h.get("margin_ratio", 0.6)
            h["own_cost"] = round(h["cost"] * (1 - ratio), 0)
            h["loan"]     = round(h["cost"] * ratio, 0)
            if h.get("own_cost") and h["own_cost"] > 0:
                h["margin_pnl_pct"] = round((h["market_value"] - h["cost"]) / h["own_cost"] * 100, 2)
            try:
                days = (datetime.now() - datetime.strptime(h["entry_date"], "%Y-%m-%d")).days
                daily = round(h["loan"] * 0.0635 / 365, 0)
                h.update({"margin_days": days, "margin_daily_interest": daily,
                          "margin_total_interest": round(daily * days, 0)})
            except:
                h.update({"margin_days": 0, "margin_daily_interest": 0, "margin_total_interest": 0})

        is_lev = any(k in h.get("ticker","").upper() or k in h.get("name","") for k in LEVERAGED_KEYWORDS)
        if is_lev:
            try:
                days = (datetime.now() - datetime.strptime(h["entry_date"], "%Y-%m-%d")).days
                decay = round(days * 0.03, 2)
                h.update({"is_leveraged": True, "leverage_days": days,
                          "leverage_decay_pct": decay, "leverage_warning": decay > 5})
            except:
                h.update({"is_leveraged": True, "leverage_decay_pct": 0})

        holdings.append(h)
    return holdings

@router.post("")
async def add_holding(holding: Holding):
    data = holding.dict()
    live = get_stock_data(holding.ticker, holding.market)
    if live:
        data.update(live)
        data["status"] = "ok"
    data["created_at"] = datetime.utcnow().isoformat()
    result = await db.holdings.insert_one(data)
    return {"id": str(result.inserted_id), "message": "持倉新增成功", "stock_data": live}

@router.put("/{holding_id}")
async def update_holding(holding_id: str, update: HoldingUpdate):
    update_data = {k: v for k, v in update.dict().items() if v is not None}
    update_data["updated_at"] = datetime.utcnow().isoformat()
    await db.holdings.update_one({"_id": ObjectId(holding_id)}, {"$set": update_data})
    return {"message": "更新成功"}

@router.delete("/{holding_id}")
async def delete_holding(holding_id: str):
    await db.holdings.delete_one({"_id": ObjectId(holding_id)})
    return {"message": "持倉刪除成功"}

@router.put("/{holding_id}/note")
async def update_note(holding_id: str, body: NoteUpdate):
    await db.holdings.update_one(
        {"_id": ObjectId(holding_id)},
        {"$set": {"note": body.note, "note_updated_at": datetime.utcnow().isoformat()}}
    )
    return {"message": "筆記更新成功"}
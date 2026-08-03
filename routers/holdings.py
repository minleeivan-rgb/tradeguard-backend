import asyncio
from fastapi import APIRouter, HTTPException
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime
from database import db
from models import Holding, HoldingUpdate, NoteUpdate
from services.yfinance_service import get_stock_data, calculate_status, is_tw_trading_hours

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
    # FIX: 原本 {"name": user_id}，改為 {"email": user_id}
    user  = await db.users.find_one({"email": user_id})
    rules = user.get("rules", {}) if user else {}

    async for h in db.holdings.find({"user_id": user_id}):
        h = fix_id(h)

        if refresh_prices:
            live = None
            if h["market"] == "tw":
                from services.finmind import get_tw_stock_price
                from services.yfinance_service import get_tw_realtime_price
                live = await get_tw_stock_price(h["ticker"])
                # FIX: 任何時段都先抓即時價（TWSE MIS → Yahoo 雙備援），並記錄報價時間
                rt = await get_tw_realtime_price(h["ticker"])
                if rt and rt.get("current_price"):
                    if not live:
                        live = {"current_price": rt["current_price"], "highest_price": 0,
                                "ma20": None, "ma60": None,
                                "ma20_diff_pct": None, "ma60_diff_pct": None}
                    live["current_price"] = rt["current_price"]
                    live["price_time"]    = rt.get("price_time", "")
                    live["price_source"]  = rt.get("source", "")
                    # FIX: 用即時價重算均線乖離，避免顯示過期乖離造成誤判
                    if live.get("ma20"):
                        live["ma20_diff_pct"] = round((rt["current_price"] - live["ma20"]) / live["ma20"] * 100, 2)
                    if live.get("ma60"):
                        live["ma60_diff_pct"] = round((rt["current_price"] - live["ma60"]) / live["ma60"] * 100, 2)
                elif live:
                    live["price_time"]   = "最近收盤"
                    live["price_source"] = "FinMind日線"
            else:
                # FIX: get_stock_data 是同步函數，用 asyncio.to_thread 避免阻塞 event loop
                live = await asyncio.to_thread(get_stock_data, h["ticker"], h["market"])
                if live:
                    live["price_time"]   = "最近收盤"
                    live["price_source"] = "yfinance"

            if not live:
                from services.twse import _twse_cache
                perf = _twse_cache.get("performance", {})
                if h["ticker"] in perf:
                    p = perf[h["ticker"]]
                    live = {"current_price": p["current_price"], "highest_price": p["current_price"],
                            "ma20": None, "ma60": None, "ma20_diff_pct": None, "ma60_diff_pct": None}
            if live:
                current = live["current_price"]
                # FIX: 不再使用 yfinance 抓的歷史最高（包含買入前的價格）
                # 只用「現價 vs 資料庫已記錄最高」取大值，追蹤持倉期間最高點
                stored_highest = h.get("highest_price", 0)
                highest = max(current, stored_highest) if stored_highest > 0 else current
                status  = calculate_status(current, h["entry_price"], highest, rules)
                # FIX: ObjectId 保護
                try:
                    oid = ObjectId(h["_id"])
                except InvalidId:
                    oid = None
                if oid:
                    await db.holdings.update_one(
                        {"_id": oid},
                        {"$set": {"current_price": current, "highest_price": highest,
                                  "ma20": live["ma20"], "ma60": live["ma60"],
                                  "ma20_diff_pct": live["ma20_diff_pct"],
                                  "ma60_diff_pct": live["ma60_diff_pct"],
                                  "status": status,
                                  "price_time": live.get("price_time", ""),
                                  "price_source": live.get("price_source", ""),
                                  "updated_at": datetime.utcnow().isoformat()}}
                    )
                # FIX: 過濾掉 raw_closes/raw_rows（幾百筆原始資料），避免 response 過大
                safe_live = {k: v for k, v in live.items() if k not in ("raw_closes", "raw_rows")}
                h.update({"current_price": current, "highest_price": highest, "status": status, **safe_live})

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
    live = None
    # FIX: 台股優先用 FinMind（資料較準），失敗才 fallback 到 yfinance
    if holding.market == "tw":
        from services.finmind import get_tw_stock_price
        live = await get_tw_stock_price(holding.ticker)
    if not live:
        live = await asyncio.to_thread(get_stock_data, holding.ticker, holding.market)
    if live:
        current = live["current_price"]
        # FIX: 只寫入安全欄位，避免 raw_closes/raw_rows 存進 DB
        data["current_price"] = current
        data["ma20"] = live.get("ma20")
        data["ma60"] = live.get("ma60")
        data["ma20_diff_pct"] = live.get("ma20_diff_pct")
        data["ma60_diff_pct"] = live.get("ma60_diff_pct")
        # FIX: 最高點從進場價或現價取大值，不用歷史最高
        data["highest_price"] = max(current, holding.entry_price)
        data["status"] = "ok"
    data["created_at"] = datetime.utcnow().isoformat()
    result = await db.holdings.insert_one(data)
    return {"id": str(result.inserted_id), "message": "持倉新增成功"}

@router.put("/{holding_id}")
async def update_holding(holding_id: str, update: HoldingUpdate):
    # FIX: ObjectId 格式保護
    try:
        oid = ObjectId(holding_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="holding_id 格式不正確")
    update_data = {k: v for k, v in update.dict().items() if v is not None}
    update_data["updated_at"] = datetime.utcnow().isoformat()
    await db.holdings.update_one({"_id": oid}, {"$set": update_data})
    return {"message": "更新成功"}

@router.delete("/{holding_id}")
async def delete_holding(holding_id: str):
    # FIX: ObjectId 格式保護
    try:
        oid = ObjectId(holding_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="holding_id 格式不正確")
    await db.holdings.delete_one({"_id": oid})
    return {"message": "持倉刪除成功"}

@router.put("/{holding_id}/note")
async def update_note(holding_id: str, body: NoteUpdate):
    # FIX: ObjectId 格式保護
    try:
        oid = ObjectId(holding_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="holding_id 格式不正確")
    await db.holdings.update_one(
        {"_id": oid},
        {"$set": {"note": body.note, "note_updated_at": datetime.utcnow().isoformat()}}
    )
    return {"message": "筆記更新成功"}

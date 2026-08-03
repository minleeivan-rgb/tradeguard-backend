"""
每日全市場快照：股價 + 三大法人(寬表) 存進 MongoDB
- market_daily : {_id:"YYYY-MM-DD_ticker", date, ticker, open, high, low, close, volume, money}
- market_chips : {_id:"YYYY-MM-DD_ticker", date, ticker, trust_net, foreign_net, dealer_net}
- market_breadth: {_id:"YYYY-MM-DD", date, up, down, flat, limit_up, limit_down, adl}
資料源：FinMind Backer 全市場單日下載（TaiwanStockPrice / TaiwanStockInstitutionalInvestorsBuySellWide）
"""
import os
import asyncio
from datetime import datetime, timedelta, timezone
import httpx
from database import db

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))

_backfill_state = {"running": False, "done": 0, "total": 0, "last_date": "", "error": ""}

def tw_today() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")

async def _fm_bulk(dataset: str, date_str: str) -> list:
    """Backer 全市場單日下載（不帶 data_id）"""
    params = {"dataset": dataset, "start_date": date_str, "token": FINMIND_TOKEN}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(FINMIND_BASE, params=params)
    j = r.json()
    if j.get("status") != 200:
        raise Exception(f"FinMind {dataset} {date_str}: {j.get('msg')}")
    return j.get("data", [])

async def ensure_indexes():
    try:
        await db.market_daily.create_index([("ticker", 1), ("date", 1)])
        await db.market_daily.create_index([("date", 1)])
        await db.market_chips.create_index([("ticker", 1), ("date", 1)])
        await db.market_chips.create_index([("date", 1)])
        await db.watchlist.create_index([("user_id", 1), ("ticker", 1)], unique=True)
    except Exception as e:
        print(f"[snapshot] index: {e}")

async def snapshot_prices(date_str: str) -> int:
    rows = await _fm_bulk("TaiwanStockPrice", date_str)
    if not rows:
        return 0
    ops = []
    for r in rows:
        sid = str(r.get("stock_id", "")).strip()
        close = float(r.get("close", 0) or 0)
        if not (sid.isdigit() and 4 <= len(sid) <= 5) or close <= 0:
            continue
        ops.append({
            "_id": f"{date_str}_{sid}", "date": date_str, "ticker": sid,
            "open": float(r.get("open", 0) or 0), "high": float(r.get("max", 0) or 0),
            "low": float(r.get("min", 0) or 0), "close": close,
            "volume": float(r.get("Trading_Volume", 0) or 0),
            "money": float(r.get("Trading_money", 0) or 0),
        })
    if not ops:
        return 0
    from pymongo import ReplaceOne
    await db.market_daily.bulk_write(
        [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in ops], ordered=False)
    return len(ops)

async def snapshot_chips(date_str: str) -> int:
    rows = await _fm_bulk("TaiwanStockInstitutionalInvestorsBuySellWide", date_str)
    if not rows:
        return 0
    ops = []
    for r in rows:
        sid = str(r.get("stock_id", "")).strip()
        if not (sid.isdigit() and 4 <= len(sid) <= 5):
            continue
        def g(k):
            try:
                return float(r.get(k, 0) or 0)
            except Exception:
                return 0.0
        trust   = g("Investment_Trust_buy") - g("Investment_Trust_sell")
        foreign = (g("Foreign_Investor_buy") - g("Foreign_Investor_sell")
                   + g("Foreign_Dealer_Self_buy") - g("Foreign_Dealer_Self_sell"))
        dealer  = (g("Dealer_buy") - g("Dealer_sell")
                   + g("Dealer_self_buy") - g("Dealer_self_sell")
                   + g("Dealer_Hedging_buy") - g("Dealer_Hedging_sell"))
        ops.append({"_id": f"{date_str}_{sid}", "date": date_str, "ticker": sid,
                    "trust_net": trust, "foreign_net": foreign, "dealer_net": dealer})
    if not ops:
        return 0
    from pymongo import ReplaceOne
    await db.market_chips.bulk_write(
        [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in ops], ordered=False)
    return len(ops)

async def rebuild_breadth():
    """由 market_daily 重建整條 漲跌家數 + ADL 序列"""
    dates = await db.market_daily.distinct("date")
    dates.sort()
    if len(dates) < 2:
        return 0
    adl = 0
    count = 0
    prev_close: dict = {}
    for i, d in enumerate(dates):
        cur = {}
        async for doc in db.market_daily.find({"date": d}, {"ticker": 1, "close": 1}):
            cur[doc["ticker"]] = doc["close"]
        if i == 0:
            prev_close = cur
            continue
        up = down = flat = limit_up = limit_down = 0
        for t, c in cur.items():
            p = prev_close.get(t)
            if not p or p <= 0:
                continue
            chg = (c - p) / p * 100
            if chg > 0.001:
                up += 1
            elif chg < -0.001:
                down += 1
            else:
                flat += 1
            if chg >= 9.5:
                limit_up += 1
            elif chg <= -9.5:
                limit_down += 1
        adl += (up - down)
        await db.market_breadth.replace_one(
            {"_id": d},
            {"_id": d, "date": d, "up": up, "down": down, "flat": flat,
             "limit_up": limit_up, "limit_down": limit_down, "adl": adl},
            upsert=True)
        prev_close = cur
        count += 1
    return count

async def run_daily_snapshot() -> dict:
    """收盤後（FinMind 21:00 後資料齊）跑當日快照"""
    d = tw_today()
    now = datetime.now(TW_TZ)
    if now.weekday() >= 5:
        return {"skipped": "weekend"}
    out = {"date": d}
    try:
        out["prices"] = await snapshot_prices(d)
    except Exception as e:
        out["prices_error"] = str(e)
    try:
        out["chips"] = await snapshot_chips(d)
    except Exception as e:
        out["chips_error"] = str(e)
    try:
        out["breadth_days"] = await rebuild_breadth()
    except Exception as e:
        out["breadth_error"] = str(e)
    return out

async def run_backfill(days: int = 130):
    """回補歷史（背景執行），約 days*0.7 個交易日"""
    global _backfill_state
    if _backfill_state["running"]:
        return
    _backfill_state.update({"running": True, "done": 0, "total": 0, "error": ""})
    try:
        await ensure_indexes()
        have = set(await db.market_daily.distinct("date"))
        start = datetime.now(TW_TZ) - timedelta(days=days)
        cal = []
        cur = start
        while cur.date() <= datetime.now(TW_TZ).date():
            if cur.weekday() < 5:
                ds = cur.strftime("%Y-%m-%d")
                if ds not in have:
                    cal.append(ds)
            cur += timedelta(days=1)
        _backfill_state["total"] = len(cal)
        for ds in cal:
            try:
                n = await snapshot_prices(ds)
                if n > 0:
                    await snapshot_chips(ds)
            except Exception as e:
                print(f"[backfill] {ds}: {e}")
            _backfill_state["done"] += 1
            _backfill_state["last_date"] = ds
            await asyncio.sleep(0.4)
        await rebuild_breadth()
    except Exception as e:
        _backfill_state["error"] = str(e)
        print(f"[backfill] fatal: {e}")
    finally:
        _backfill_state["running"] = False

def backfill_state() -> dict:
    return dict(_backfill_state)

async def db_status() -> dict:
    dates = await db.market_daily.distinct("date")
    dates.sort()
    chip_dates = await db.market_chips.distinct("date")
    return {
        "daily_days": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "chips_days": len(chip_dates),
        "backfill": backfill_state(),
    }

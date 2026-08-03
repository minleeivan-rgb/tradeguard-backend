import asyncio
from fastapi import APIRouter
from datetime import datetime
from pydantic import BaseModel
from database import db

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

class WatchAdd(BaseModel):
    ticker: str
    market: str = "tw"
    name: str = ""

@router.get("/{user_id}")
async def list_watchlist(user_id: str):
    items = []
    async for w in db.watchlist.find({"user_id": user_id}, {"_id": 0}):
        items.append(w)
    items.sort(key=lambda x: x.get("added_at", ""))
    return {"items": items}

@router.post("/{user_id}")
async def add_watch(user_id: str, body: WatchAdd):
    ticker = body.ticker.strip().upper() if body.market == "us" else body.ticker.strip()
    name = body.name.strip()
    if not name and body.market == "tw":
        try:
            from services.finmind import search_tw_stock
            hits = await search_tw_stock(ticker)
            exact = [h for h in hits if h["ticker"] == ticker]
            if exact:
                name = exact[0]["name"]
            elif hits:
                name = hits[0]["name"]
        except Exception:
            pass
    doc = {"user_id": user_id, "ticker": ticker, "market": body.market,
           "name": name or ticker, "added_at": datetime.utcnow().isoformat()}
    await db.watchlist.replace_one({"user_id": user_id, "ticker": ticker}, doc, upsert=True)
    return {"ok": True, "ticker": ticker, "name": doc["name"]}

@router.delete("/{user_id}/{ticker}")
async def del_watch(user_id: str, ticker: str):
    await db.watchlist.delete_one({"user_id": user_id, "ticker": ticker})
    return {"ok": True}

@router.get("/{user_id}/scan")
async def scan_watchlist(user_id: str):
    """觀察清單即時掃描：現價(即時) + 技術 + 投信連買 + 背離"""
    from services.finmind import get_tw_technical
    from services.yfinance_service import calculate_technical_indicators, get_tw_realtime_price
    results = []
    # 投信連買 streak 一次撈
    chip_dates = await db.market_chips.distinct("date")
    chip_dates.sort()
    use = chip_dates[-8:]
    async for w in db.watchlist.find({"user_id": user_id}):
        ticker = w["ticker"]
        market = w.get("market", "tw")
        item = {"ticker": ticker, "name": w.get("name", ticker), "market": market}
        try:
            if market == "tw":
                tech = await get_tw_technical(ticker)
                rt = await get_tw_realtime_price(ticker)
                if tech:
                    cur = rt["current_price"] if rt else tech["current_price"]
                    ma = tech.get("ma", {})
                    item.update({
                        "current": cur,
                        "price_time": rt.get("price_time", "收盤") if rt else "收盤",
                        "price_source": rt.get("source", "FinMind") if rt else "FinMind",
                        "ma20_diff_pct": round((cur - ma["ma20"]) / ma["ma20"] * 100, 2) if ma.get("ma20") else None,
                        "ma60_diff_pct": round((cur - ma["ma60"]) / ma["ma60"] * 100, 2) if ma.get("ma60") else None,
                        "rsi": tech.get("rsi"), "kd": tech.get("kd", {}),
                        "direction": tech.get("direction", "中性"),
                        "volume_ratio": tech.get("volume", {}).get("ratio"),
                    })
                    kd = tech.get("kd", {})
                    rsi = tech.get("rsi", 50)
                    if kd.get("overbought") and rsi < 65:
                        item["divergence"] = {"type": "bearish", "signal": f"K{kd.get('k')} 超買但 RSI {rsi} 未跟上，頂背離風險"}
                    elif kd.get("oversold") and rsi > 35:
                        item["divergence"] = {"type": "bullish", "signal": f"K{kd.get('k')} 超賣但 RSI {rsi} 回升，底背離機會"}
                # 投信連買
                if use:
                    docs = [d async for d in db.market_chips.find(
                        {"ticker": ticker, "date": {"$in": use}}, {"date": 1, "trust_net": 1})]
                    docs.sort(key=lambda x: x["date"])
                    streak = 0
                    for dd in reversed(docs):
                        if dd.get("trust_net", 0) > 0:
                            streak += 1
                        else:
                            break
                    item["trust_streak"] = streak
            else:
                tech = await asyncio.to_thread(calculate_technical_indicators, ticker, market)
                if tech:
                    item.update({"current": tech["current_price"],
                                 "price_time": "最近收盤", "price_source": "yfinance",
                                 "ma20_diff_pct": tech.get("ma20_diff_pct"),
                                 "ma60_diff_pct": tech.get("ma60_diff_pct"),
                                 "rsi": tech.get("rsi"), "kd": tech.get("kd", {}),
                                 "direction": tech.get("direction", "中性")})
        except Exception as e:
            item["error"] = str(e)
        results.append(item)
    return {"items": results, "checked_at": datetime.utcnow().isoformat()}

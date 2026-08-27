"""
早期進場訊號掃描器（查自己的 MongoDB，不打外部 API）
- 爆量未大漲 / 均線糾結突破 / 60日新高 / 投信連買 / 族群落後補漲
- 市場內部：站上月線家數% / ADL 累積背離
- 月營收動能（FinMind 逐檔，僅掃 universe）
- 大戶持股比率（FinMind 週資料）
"""
import os
import asyncio
from datetime import datetime, timedelta, timezone
import httpx
import pandas as pd
from database import db

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))

_NAME_MAP: dict = {}


def _name(ticker: str) -> str:
    """同步版：先查已載入的全市場對照表，再退回內建表"""
    t = str(ticker)
    if _NAME_MAP.get(t):
        return _NAME_MAP[t]
    try:
        from routers.scan import TW_STOCK_LIST
        return TW_STOCK_LIST.get(t, t)
    except Exception:
        return t


async def _ensure_names():
    """載入全台股名稱對照表到模組快取（FinMind TaiwanStockInfo，6小時快取）"""
    global _NAME_MAP
    try:
        from services.finmind import get_tw_name_map
        m = await get_tw_name_map()
        if m:
            _NAME_MAP = m
    except Exception as e:
        print(f"[signals] name map: {e}")


def _fill_names(items: list) -> list:
    """把清單裡的 name 欄位補成中文（含 leader_name）"""
    for x in items or []:
        if isinstance(x, dict):
            if x.get("ticker"):
                x["name"] = _name(x["ticker"])
            if x.get("leader"):
                x["leader_name"] = _name(x["leader"])
    return items

async def _load_frames(days: int = 70):
    """回傳 (close_df, vol_df) columns=ticker index=date(sorted str)"""
    dates = await db.market_daily.distinct("date")
    dates.sort()
    use = dates[-days:]
    if len(use) < 25:
        return None, None, use
    cursor = db.market_daily.find(
        {"date": {"$in": use}}, {"date": 1, "ticker": 1, "close": 1, "volume": 1, "_id": 0})
    rows = [r async for r in cursor]
    if not rows:
        return None, None, use
    df = pd.DataFrame(rows)
    close = df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    vol   = df.pivot_table(index="date", columns="ticker", values="volume").sort_index()
    return close, vol, use

def _pct(a, b):
    try:
        return round((a - b) / b * 100, 2) if b else 0.0
    except Exception:
        return 0.0

async def scan_early() -> dict:
    await _ensure_names()
    close, vol, dates = await _load_frames(70)
    if close is None:
        return {"error": f"歷史資料不足（目前 {len(dates)} 天，需先執行回補）", "data_date": dates[-1] if dates else ""}
    latest = close.index[-1]
    c_now  = close.iloc[-1]
    c_prev = close.iloc[-2]
    chg    = (c_now / c_prev - 1) * 100
    v_now  = vol.iloc[-1]
    v_ma20 = vol.iloc[-21:-1].mean()
    vr     = v_now / v_ma20.replace(0, float("nan"))
    ma5  = close.rolling(5).mean().iloc[-1]
    ma10 = close.rolling(10).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma5y  = close.rolling(5).mean().iloc[-2]
    ma10y = close.rolling(10).mean().iloc[-2]
    ma20y = close.rolling(20).mean().iloc[-2]
    high60_prior = close.iloc[-61:-1].max()

    liquid = (v_now >= 1_000_000) & (c_now >= 10)

    # 1) 爆量未大漲
    m1 = liquid & (vr >= 2.5) & (chg >= 0) & (chg < 4)
    vol_quiet = []
    for t in c_now.index[m1.fillna(False)]:
        vol_quiet.append({"ticker": t, "name": _name(t), "close": round(float(c_now[t]), 2),
                          "chg_pct": round(float(chg[t]), 2), "vol_ratio": round(float(vr[t]), 2)})
    vol_quiet.sort(key=lambda x: -x["vol_ratio"])

    # 2) 均線糾結突破
    sq_spread = (pd.concat([ma5y, ma10y, ma20y], axis=1).max(axis=1)
                 - pd.concat([ma5y, ma10y, ma20y], axis=1).min(axis=1)) \
                / pd.concat([ma5y, ma10y, ma20y], axis=1).min(axis=1) * 100
    m2 = liquid & (sq_spread < 3) & (c_now > pd.concat([ma5, ma10, ma20], axis=1).max(axis=1)) \
         & (chg >= 2) & (vr >= 1.5)
    squeeze = []
    for t in c_now.index[m2.fillna(False)]:
        squeeze.append({"ticker": t, "name": _name(t), "close": round(float(c_now[t]), 2),
                        "chg_pct": round(float(chg[t]), 2), "vol_ratio": round(float(vr[t]), 2),
                        "squeeze_pct": round(float(sq_spread[t]), 2)})
    squeeze.sort(key=lambda x: -x["vol_ratio"])

    # 3) 60日新高
    m3 = liquid & (c_now >= high60_prior) & (vr >= 1.3) & (chg >= 1)
    high60 = []
    for t in c_now.index[m3.fillna(False)]:
        high60.append({"ticker": t, "name": _name(t), "close": round(float(c_now[t]), 2),
                       "chg_pct": round(float(chg[t]), 2), "vol_ratio": round(float(vr[t]), 2)})
    high60.sort(key=lambda x: -x["chg_pct"])

    # 4) 投信連買（且 5 日漲幅 < 8% 還沒噴）
    trust = await _scan_trust_streak(close)

    # 5) 族群落後補漲
    laggards = await scan_laggards(close)

    return {"data_date": latest,
            "vol_quiet": _fill_names(vol_quiet[:15]),
            "squeeze": _fill_names(squeeze[:15]),
            "high60": _fill_names(high60[:15]),
            "trust": _fill_names(trust[:15]),
            "laggards": _fill_names(laggards[:12])}

async def _scan_trust_streak(close: pd.DataFrame) -> list:
    chip_dates = await db.market_chips.distinct("date")
    chip_dates.sort()
    use = chip_dates[-8:]
    if len(use) < 3:
        return []
    cursor = db.market_chips.find({"date": {"$in": use}},
                                  {"date": 1, "ticker": 1, "trust_net": 1, "_id": 0})
    rows = [r async for r in cursor]
    if not rows:
        return []
    df = pd.DataFrame(rows).pivot_table(index="date", columns="ticker", values="trust_net").sort_index()
    out = []
    c5ago = close.iloc[-6] if len(close) >= 6 else close.iloc[0]
    c_now = close.iloc[-1]
    for t in df.columns:
        s = df[t].fillna(0)
        streak = 0
        total = 0.0
        for v in reversed(s.tolist()):
            if v > 0:
                streak += 1
                total += v
            else:
                break
        if streak >= 3 and t in c_now.index:
            chg5 = _pct(float(c_now[t]), float(c5ago.get(t, c_now[t])))
            if chg5 < 8:
                out.append({"ticker": t, "name": _name(t), "streak": streak,
                            "total_lots": round(total / 1000, 0),
                            "close": round(float(c_now[t]), 2), "chg5_pct": chg5})
    out.sort(key=lambda x: (-x["streak"], -x["total_lots"]))
    return out

async def scan_laggards(close: pd.DataFrame = None) -> list:
    await _ensure_names()
    if close is None:
        close, _, _ = await _load_frames(40)
        if close is None:
            return []
    try:
        from routers.scan import TW_THEME_SECTORS
    except Exception:
        return []
    if len(close) < 21:
        return []
    c_now, c_20 = close.iloc[-1], close.iloc[-21]
    ma20 = close.rolling(20).mean().iloc[-1]
    out = []
    for sector, tickers in TW_THEME_SECTORS.items():
        perf = []
        for t in tickers:
            if t in c_now.index and pd.notna(c_now[t]) and pd.notna(c_20.get(t)):
                perf.append((t, _pct(float(c_now[t]), float(c_20[t]))))
        if len(perf) < 3:
            continue
        perf.sort(key=lambda x: -x[1])
        leader, lead_ret = perf[0]
        if lead_ret < 15:
            continue
        for t, r in perf[1:]:
            if r <= lead_ret * 0.5 and t in ma20.index and pd.notna(ma20[t]) \
               and float(c_now[t]) > float(ma20[t]):
                out.append({"sector": sector, "leader": leader, "leader_name": _name(leader),
                            "leader_ret20": round(lead_ret, 1),
                            "ticker": t, "name": _name(t), "ret20": round(r, 1),
                            "close": round(float(c_now[t]), 2)})
    out.sort(key=lambda x: -x["leader_ret20"])
    return out

async def pct_above_ma20() -> dict:
    close, _, dates = await _load_frames(30)
    if close is None:
        return {"pct": None, "note": f"歷史 {len(dates)} 天不足"}
    ma20 = close.rolling(20).mean().iloc[-1]
    c    = close.iloc[-1]
    valid = c.notna() & ma20.notna()
    n = int(valid.sum())
    if n == 0:
        return {"pct": None}
    above = int(((c > ma20) & valid).sum())
    pct = round(above / n * 100, 1)
    label = "過熱" if pct > 80 else "偏弱洗盤完成區" if pct < 20 else "中性"
    return {"pct": pct, "above": above, "total": n, "label": label,
            "universe": "上市+上櫃+興櫃(FinMind)", "data_date": close.index[-1]}

async def adl_status() -> dict:
    docs = [d async for d in db.market_breadth.find({}, {"_id": 0}).sort("date", 1)]
    if len(docs) < 25:
        return {"note": f"ADL 需更多天數（目前 {len(docs)}）"}
    adl = [d["adl"] for d in docs]
    dates = [d["date"] for d in docs]
    latest, prev5 = adl[-1], adl[-6]
    # 指數 60 日高 vs ADL 60 日高 背離
    div = None
    try:
        import yfinance as yf
        hist = yf.Ticker("^TWII").history(period="4mo")["Close"]
        idx_high = float(hist.iloc[-1]) >= float(hist.tail(60).max()) * 0.995
        adl_high = latest >= max(adl[-60:]) * 0.999 if len(adl) >= 60 else latest >= max(adl)
        if idx_high and not adl_high:
            div = "指數逼近波段高點但 ADL 未同步創高 → 內部結構弱化（頂背離），漲勢集中在少數權值股"
    except Exception:
        pass
    return {"adl": latest, "chg5": latest - prev5, "date": dates[-1], "divergence": div}

async def revenue_momentum(tickers: list) -> list:
    """月營收動能：YoY 連續擴大或創高（逐檔查 FinMind）"""
    await _ensure_names()
    start = (datetime.now(TW_TZ) - timedelta(days=460)).strftime("%Y-%m-%d")
    out = []
    async with httpx.AsyncClient(timeout=15) as c:
        for t in tickers:
            try:
                r = await c.get(FINMIND_BASE, params={
                    "dataset": "TaiwanStockMonthRevenue", "data_id": t,
                    "start_date": start, "token": FINMIND_TOKEN})
                rows = r.json().get("data", [])
                if len(rows) < 14:
                    continue
                rows.sort(key=lambda x: (x["revenue_year"], x["revenue_month"]))
                rev = {(x["revenue_year"], x["revenue_month"]): x["revenue"] for x in rows}
                yoy = []
                for x in rows[-4:]:
                    prev = rev.get((x["revenue_year"] - 1, x["revenue_month"]))
                    if prev:
                        yoy.append(round((x["revenue"] - prev) / prev * 100, 1))
                if len(yoy) < 3:
                    continue
                accelerating = yoy[-1] > yoy[-2] > yoy[-3] and yoy[-1] > 0
                strong = yoy[-1] >= 30
                latest_rev = rows[-1]["revenue"]
                rec_high = latest_rev >= max(x["revenue"] for x in rows[-13:])
                if accelerating or (strong and rec_high):
                    out.append({"ticker": t, "name": _name(t), "yoy": yoy[-3:],
                                "accelerating": accelerating, "record_high": rec_high,
                                "month": f"{rows[-1]['revenue_year']}/{rows[-1]['revenue_month']}"})
                await asyncio.sleep(0.15)
            except Exception:
                continue
    out.sort(key=lambda x: -x["yoy"][-1])
    return out

async def holders_ratio(ticker: str) -> dict:
    """大戶（400張以上）持股比率近 4 週變化"""
    await _ensure_names()
    start = (datetime.now(TW_TZ) - timedelta(days=70)).strftime("%Y-%m-%d")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(FINMIND_BASE, params={
            "dataset": "TaiwanStockHoldingSharesPer", "data_id": ticker,
            "start_date": start, "token": FINMIND_TOKEN})
    rows = r.json().get("data", [])
    if not rows:
        return {"error": "無資料"}
    big = {}
    for x in rows:
        lvl = str(x.get("HoldingSharesLevel", ""))
        digits = "".join(ch for ch in lvl.split("-")[0] if ch.isdigit())
        try:
            low = int(digits) if digits else 0
        except Exception:
            low = 0
        if low >= 400001 or "1,000,001" in lvl or "more" in lvl.lower():
            d = x["date"]
            big[d] = big.get(d, 0) + float(x.get("percent", 0) or 0)
    series = sorted(big.items())[-5:]
    if not series:
        return {"error": "無大戶級距資料"}
    delta = round(series[-1][1] - series[0][1], 2) if len(series) > 1 else 0
    return {"ticker": ticker, "name": _name(ticker),
            "series": [{"date": d, "pct": round(p, 2)} for d, p in series],
            "delta": delta,
            "trend": "集中" if delta > 0.3 else "分散" if delta < -0.3 else "持平"}

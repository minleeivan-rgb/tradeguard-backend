import os
import asyncio
from fastapi import APIRouter
from datetime import datetime, timedelta
import httpx
from database import db

router = APIRouter(prefix="/market", tags=["market"])

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"

INTL_INDICES = {
    "sp500":  {"ticker": "^GSPC",    "name": "S&P 500"},
    "nasdaq": {"ticker": "^IXIC",    "name": "NASDAQ"},
    "sox":    {"ticker": "^SOX",     "name": "費半 SOX"},
    "nikkei": {"ticker": "^N225",    "name": "日經 225"},
    "kospi":  {"ticker": "^KS11",    "name": "韓國 KOSPI"},
    "vix":    {"ticker": "^VIX",     "name": "VIX 恐慌"},
    "dxy":    {"ticker": "DX-Y.NYB", "name": "美元指數"},
    "us10y":  {"ticker": "^TNX",     "name": "美債10年"},
    "usdtwd": {"ticker": "USDTWD=X", "name": "美元/台幣"},
}

def _yf_data(ticker: str) -> dict | None:
    import yfinance as yf
    try:
        hist = yf.Ticker(ticker).history(period="3mo")
        if hist.empty or len(hist) < 14:
            return None
        closes = hist["Close"]
        cur  = round(float(closes.iloc[-1]), 4)
        prev = round(float(closes.iloc[-2]), 4)
        chg  = round((cur - prev) / prev * 100, 2)
        delta = closes.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = round(float(100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1]))), 1) \
                if loss.iloc[-1] > 0 else 50.0
        lo9 = closes.rolling(9).min()
        hi9 = closes.rolling(9).max()
        rsv = (closes - lo9) / (hi9 - lo9) * 100
        k   = rsv.ewm(com=2).mean()
        d   = k.ewm(com=2).mean()
        return {
            "current": cur, "change_pct": chg, "rsi": rsi,
            "kd": {"k": round(float(k.iloc[-1]), 1), "d": round(float(d.iloc[-1]), 1)},
            "closes": closes.tolist()[-60:],
        }
    except Exception as e:
        print(f"[yf] {ticker}: {e}")
        return None

def _divergence(closes: list, rsi: float) -> dict:
    if len(closes) < 20:
        return {"type": "none", "signal": None}
    peak    = max(closes)
    trough  = min(closes)
    current = closes[-1]
    if current >= peak * 0.97 and rsi < 65:
        return {"type": "bearish", "signal": f"接近近期高點但 RSI={rsi} 動能不足，注意風險"}
    if current <= trough * 1.03 and rsi > 35:
        return {"type": "bullish", "signal": f"接近近期低點但 RSI={rsi} 動能回升，可能反彈"}
    return {"type": "none", "signal": None}

# ── FinMind 整體市場資料（自帶，正確資料集）──────────────────────

async def _fm_total(dataset: str, days: int = 30) -> list:
    """整體市場專用資料集，不需要 data_id"""
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end   = datetime.now().strftime("%Y-%m-%d")
    params = {"dataset": dataset, "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(FINMIND_BASE, params=params)
    j = r.json()
    if j.get("status") != 200:
        raise Exception(f"FinMind {dataset}: {j.get('msg', 'unknown')}")
    return j.get("data", [])

def _to_yi(raw: float) -> float:
    """自動判斷單位（元或仟元）換算成億"""
    for div in (1e8, 1e5):
        v = raw / div
        if 100 <= v <= 100000:
            return round(v, 1)
    return round(raw / 1e8, 1)

def _safe_float(s):
    try:
        return float(str(s).replace(",", ""))
    except:
        return 0

async def _margin_trend_data() -> dict | None:
    """融資餘額趨勢（FinMind TaiwanStockTotalMarginPurchaseShortSale）"""
    rows = await _fm_total("TaiwanStockTotalMarginPurchaseShortSale", days=45)
    m = [r for r in rows if r.get("name") == "MarginPurchaseMoney"]
    m.sort(key=lambda x: x["date"])
    m = m[-20:]
    if not m:
        return None
    history, prev = [], None
    for r in m:
        bal = _to_yi(float(r.get("TodayBalance", 0)))
        chg = round((bal - prev) / prev * 100, 2) if prev else 0
        history.append({"date": r["date"], "balance": bal, "change_pct": chg})
        prev = bal
    latest = history[-1]
    return {"balance": latest["balance"], "change_pct": latest["change_pct"],
            "trend": "增加" if latest["change_pct"] > 0 else "減少",
            "history": history}

# ── Endpoints ────────────────────────────────────────────────────

@router.get("/indices")
async def get_indices():
    results = {}
    for key, info in INTL_INDICES.items():
        data = await asyncio.to_thread(_yf_data, info["ticker"])
        if not data:
            continue
        div = _divergence(data["closes"], data["rsi"])
        vix_status = None
        if key == "vix":
            v = data["current"]
            vix_status = "danger" if v >= 30 else "warning" if v >= 20 else "normal"
        results[key] = {
            "name": info["name"], "current": data["current"],
            "change_pct": data["change_pct"], "rsi": data["rsi"],
            "kd": data["kd"], "divergence": div, "vix_status": vix_status,
        }
    return {"indices": results, "updated_at": datetime.utcnow().isoformat()}

@router.get("/tw")
async def get_tw_market():
    tw_index = None
    try:
        data = await asyncio.to_thread(_yf_data, "^TWII")
        if data:
            tw_index = {"name": "台股加權指數",
                        "current": round(data["current"], 0),
                        "change_pct": data["change_pct"],
                        "rsi": data["rsi"], "kd": data["kd"]}
    except Exception as e:
        print(f"[market] TWII: {e}")

    tw_futures = None
    try:
        from services.finmind import get_tw_futures_data
        tw_futures = await get_tw_futures_data()
    except Exception as e:
        print(f"[market] futures: {e}")

    breadth, breadth_err = None, None
    try:
        from services.twse import fetch_twse_stock_performance
        perf = await fetch_twse_stock_performance()
        if perf:
            up   = sum(1 for v in perf.values() if v["change_pct"] > 0)
            down = sum(1 for v in perf.values() if v["change_pct"] < 0)
            flat = len(perf) - up - down
            limit_up   = sum(1 for v in perf.values() if v.get("to_limit_pct") is not None and v.get("to_limit_pct") <= 0.1)
            limit_down = sum(1 for v in perf.values() if v["change_pct"] <= -9.5)
            ratio = round(up / down, 2) if down > 0 else 99
            breadth = {"up": up, "down": down, "flat": flat,
                       "limit_up": limit_up, "limit_down": limit_down, "ratio": ratio,
                       "breadth": "強勢" if ratio > 2 else "弱勢" if ratio < 0.5 else "平衡"}
    except Exception as e:
        breadth_err = str(e)
        print(f"[market] breadth: {e}")

    margin = None
    try:
        mt = await _margin_trend_data()
        if mt:
            margin = {"balance": mt["balance"], "change_pct": mt["change_pct"], "trend": mt["trend"]}
    except Exception as e:
        print(f"[market] margin: {e}")

    return {"tw_index": tw_index, "tw_futures": tw_futures,
            "breadth": breadth, "breadth_error": breadth_err, "margin": margin,
            "updated_at": datetime.utcnow().isoformat()}

@router.get("/institutional")
async def get_institutional():
    """三大法人：FinMind 整體資料集為主（金額/元），T86 為備援（股數）"""
    fm_err = None
    try:
        rows = await _fm_total("TaiwanStockTotalInstitutionalInvestors", days=10)
        if rows:
            latest_date = max(r["date"] for r in rows)
            today = [r for r in rows if r["date"] == latest_date]
            def net(names):
                return sum(float(r.get("buy", 0)) - float(r.get("sell", 0))
                           for r in today if r.get("name") in names)
            f = net({"Foreign_Investor", "Foreign_Dealer_Self"})
            t = net({"Investment_Trust"})
            d = net({"Dealer_self", "Dealer_Hedging"})
            return {"date": latest_date, "source": "finmind", "unit": "元",
                    "foreign_net": f, "foreign_trend": "買超" if f > 0 else "賣超",
                    "trust_net": t,   "trust_trend":   "買超" if t > 0 else "賣超",
                    "dealer_net": d,  "dealer_trend":  "買超" if d > 0 else "賣超"}
    except Exception as e:
        fm_err = str(e)

    # 備援：TWSE T86（單位是股數，索引已修正：外資=4、投信=10、自營=11）
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL"
        async with httpx.AsyncClient(timeout=15, verify=False) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
        j = r.json()
        rows = j.get("data", [])
        if not rows:
            return {"error": f"finmind: {fm_err} | t86: 今日無資料"}
        f = sum(_safe_float(x[4])  for x in rows if len(x) > 4)
        t = sum(_safe_float(x[10]) for x in rows if len(x) > 10)
        d = sum(_safe_float(x[11]) for x in rows if len(x) > 11)
        return {"date": j.get("date", ""), "source": "t86", "unit": "股",
                "fm_error": fm_err,
                "foreign_net": f, "foreign_trend": "買超" if f > 0 else "賣超",
                "trust_net": t,   "trust_trend":   "買超" if t > 0 else "賣超",
                "dealer_net": d,  "dealer_trend":  "買超" if d > 0 else "賣超"}
    except Exception as e2:
        return {"error": f"finmind: {fm_err} | t86: {e2}"}

@router.get("/margin-trend")
async def get_margin_trend():
    try:
        result = await _margin_trend_data()
        if result:
            return result
        return {"error": "FinMind 無融資金額資料"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/sectors")
async def get_sector_strength():
    try:
        from services.twse import fetch_twse_stock_performance
        from routers.scan import TW_THEME_SECTORS, TW_STOCK_LIST, _calc_score
        perf = await fetch_twse_stock_performance()
        if not perf:
            return {"error": "TWSE 回傳空資料", "sectors": []}
        results = []
        for sector_name, tickers in TW_THEME_SECTORS.items():
            stocks, tvol = [], 0
            for code in tickers:
                if code not in perf:
                    continue
                p = perf[code]
                vol = p.get("volume", 0)
                tvol += vol
                stocks.append({"ticker": code, "name": TW_STOCK_LIST.get(code, code),
                               "current_price": p["current_price"], "change_pct": p["change_pct"],
                               "volume": vol, "to_limit_pct": p.get("to_limit_pct"),
                               "is_hot": p["change_pct"] >= 7})
            if len(stocks) >= 2:
                results.append(_calc_score(sector_name, stocks, tvol))
        results.sort(key=lambda x: x["strength_score"], reverse=True)
        return {"sectors": results}
    except Exception as e:
        return {"error": str(e), "sectors": []}

@router.get("/margin-health/{user_id}")
async def get_margin_health(user_id: str):
    items = []
    async for h in db.holdings.find({"user_id": user_id, "margin": True}):
        current = h.get("current_price") or h["entry_price"]
        shares  = h.get("shares", 0) * (1000 if h.get("unit") == "lot" else 1)
        if shares <= 0:
            continue
        mv   = current * shares
        cost = h["entry_price"] * shares
        loan = cost * h.get("margin_ratio", 0.6)
        if loan <= 0:
            continue
        maint = round(mv / loan * 100, 1)
        call  = round((loan * 1.3) / shares, 2)
        items.append({"ticker": h["ticker"], "name": h.get("name", h["ticker"]),
                      "current": current, "market_value": round(mv, 0),
                      "loan": round(loan, 0), "maintenance_pct": maint, "call_price": call,
                      "status": "danger" if maint < 130 else "warning" if maint < 160 else "safe"})
    items.sort(key=lambda x: x["maintenance_pct"])
    return {"holdings": items, "checked_at": datetime.utcnow().isoformat()}

@router.get("/holdings-divergence/{user_id}")
async def get_holdings_divergence(user_id: str):
    from services.finmind import get_tw_technical
    from services.yfinance_service import calculate_technical_indicators
    results = []
    async for h in db.holdings.find({"user_id": user_id}):
        ticker = h["ticker"]
        market = h.get("market", "tw")
        tech = await get_tw_technical(ticker) if market == "tw" else \
               await asyncio.to_thread(calculate_technical_indicators, ticker, market)
        if not tech:
            continue
        rsi = tech.get("rsi", 50)
        kd  = tech.get("kd", {})
        div = {"type": "none", "signal": None}
        if kd.get("overbought") and rsi < 65:
            div = {"type": "bearish", "signal": "KD 超買但 RSI 動能不足，頂背離風險"}
        elif kd.get("oversold") and rsi > 35:
            div = {"type": "bullish", "signal": "KD 超賣但 RSI 回升，底背離反彈機會"}
        results.append({"ticker": ticker, "name": h.get("name", ticker),
                        "current": tech["current_price"], "rsi": rsi, "kd": kd,
                        "direction": tech.get("direction", "中性"), "divergence": div})
    return {"stocks": results}

@router.get("/debug")
async def market_debug():
    """診斷端點：測每個資料源，直接看是哪裡壞"""
    out = {"finmind_token_set": bool(FINMIND_TOKEN)}

    try:
        d = await asyncio.to_thread(_yf_data, "^TWII")
        out["twii_yfinance"] = {"ok": bool(d), "current": d["current"] if d else None}
    except Exception as e:
        out["twii_yfinance"] = {"ok": False, "error": str(e)}

    try:
        from services.twse import fetch_twse_stock_performance
        perf = await fetch_twse_stock_performance()
        out["twse_stock_day_all"] = {"ok": bool(perf), "rows": len(perf)}
    except Exception as e:
        out["twse_stock_day_all"] = {"ok": False, "error": str(e)}

    try:
        from routers.scan import TW_THEME_SECTORS, TW_STOCK_LIST, _calc_score
        out["scan_import"] = {"ok": True, "sector_count": len(TW_THEME_SECTORS)}
    except Exception as e:
        out["scan_import"] = {"ok": False, "error": str(e)}

    try:
        rows = await _fm_total("TaiwanStockTotalInstitutionalInvestors", days=7)
        out["finmind_institutional"] = {"ok": True, "rows": len(rows),
                                        "sample": rows[-1] if rows else None}
    except Exception as e:
        out["finmind_institutional"] = {"ok": False, "error": str(e)}

    try:
        rows = await _fm_total("TaiwanStockTotalMarginPurchaseShortSale", days=7)
        out["finmind_margin"] = {"ok": True, "rows": len(rows),
                                 "sample": rows[-1] if rows else None}
    except Exception as e:
        out["finmind_margin"] = {"ok": False, "error": str(e)}

    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL"
        async with httpx.AsyncClient(timeout=15, verify=False) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
        j = r.json()
        out["twse_t86"] = {"ok": j.get("stat") == "OK", "rows": len(j.get("data", []))}
    except Exception as e:
        out["twse_t86"] = {"ok": False, "error": str(e)}

    return out

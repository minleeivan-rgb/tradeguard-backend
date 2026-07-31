import asyncio
from fastapi import APIRouter
from datetime import datetime
from database import db

router = APIRouter(prefix="/market", tags=["market"])

# ── 國際指數 ──────────────────────────────────────────────────────
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

def _fetch_yf_data(ticker: str) -> dict | None:
    """同步：yfinance 抓資料 + 計算 RSI/KD"""
    import yfinance as yf
    import pandas as pd
    try:
        hist = yf.Ticker(ticker).history(period="3mo")
        if hist.empty or len(hist) < 14:
            return None
        closes = hist["Close"]
        current = round(float(closes.iloc[-1]), 4)
        prev    = round(float(closes.iloc[-2]), 4)
        change_pct = round((current - prev) / prev * 100, 2)

        # RSI(14)
        delta = closes.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi = round(float(100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1]))), 1) \
              if loss.iloc[-1] > 0 else 50.0

        # KD(9)
        low9  = closes.rolling(9).min()
        high9 = closes.rolling(9).max()
        rsv   = (closes - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2).mean()
        d = k.ewm(com=2).mean()
        k_val = round(float(k.iloc[-1]), 1)
        d_val = round(float(d.iloc[-1]), 1)

        return {
            "current": current,
            "change_pct": change_pct,
            "rsi": rsi,
            "kd": {"k": k_val, "d": d_val},
            "closes": closes.tolist()[-60:],
        }
    except Exception as e:
        print(f"[yfinance] {ticker}: {e}")
        return None

def _detect_divergence(closes: list, rsi: float, kd_k: float) -> dict:
    """
    簡化背離偵測：
    - 頂背離：近期價格在高位（接近60日高點），但 RSI < 65 或 KD > 80 卻無法繼續創高
    - 底背離：近期價格在低位（接近60日低點），但 RSI > 35 或 KD < 20 卻開始回升
    """
    if len(closes) < 20:
        return {"type": "none", "signal": None}

    recent = closes[-20:]
    peak_60 = max(closes)
    trough_60 = min(closes)
    current = closes[-1]

    near_peak   = current >= peak_60 * 0.97
    near_trough = current <= trough_60 * 1.03

    if near_peak and rsi < 65:
        return {"type": "bearish", "signal": f"頂背離：接近近期高點但 RSI={rsi} 動能不足，注意風險"}
    if near_trough and rsi > 35:
        return {"type": "bullish", "signal": f"底背離：接近近期低點但 RSI={rsi} 動能回升，可能反彈"}
    return {"type": "none", "signal": None}


@router.get("/indices")
async def get_indices():
    """國際指數 + 背離偵測"""
    results = {}
    for key, info in INTL_INDICES.items():
        data = await asyncio.to_thread(_fetch_yf_data, info["ticker"])
        if not data:
            continue
        div = _detect_divergence(data["closes"], data["rsi"], data["kd"]["k"])
        vix_status = None
        if key == "vix":
            v = data["current"]
            vix_status = "danger" if v >= 30 else "warning" if v >= 20 else "normal"
        results[key] = {
            "name":        info["name"],
            "current":     data["current"],
            "change_pct":  data["change_pct"],
            "rsi":         data["rsi"],
            "kd":          data["kd"],
            "divergence":  div,
            "vix_status":  vix_status,
        }
    return {"indices": results, "updated_at": datetime.utcnow().isoformat()}


@router.get("/tw")
async def get_tw_market():
    """台灣市場：加權指數、台指期、融資、三大法人、漲跌家數"""
    from services.finmind import (
        get_tw_index_data, get_tw_futures_data,
        get_tw_margin_balance, get_tw_institutional
    )
    from services.twse import fetch_twse_stock_performance

    # 漲跌家數從 TWSE performance 計算（免費、即時）
    perf = await fetch_twse_stock_performance()
    breadth = None
    if perf:
        up   = sum(1 for v in perf.values() if v["change_pct"] > 0)
        down = sum(1 for v in perf.values() if v["change_pct"] < 0)
        flat = len(perf) - up - down
        limit_up   = sum(1 for v in perf.values() if v.get("to_limit_pct", 999) <= 0.1)
        limit_down = sum(1 for v in perf.values() if v["change_pct"] <= -9.5)
        ratio = round(up / down, 2) if down > 0 else 99
        breadth = {
            "up": up, "down": down, "flat": flat,
            "limit_up": limit_up, "limit_down": limit_down,
            "ratio": ratio,
            "breadth": "強勢" if ratio > 2 else "弱勢" if ratio < 0.5 else "平衡",
        }

    tw_index      = await get_tw_index_data()
    tw_futures    = await get_tw_futures_data()
    margin        = await get_tw_margin_balance()
    institutional = await get_tw_institutional()

    return {
        "tw_index":     tw_index,
        "tw_futures":   tw_futures,
        "margin":       margin,
        "institutional": institutional,
        "breadth":      breadth,
        "updated_at":   datetime.utcnow().isoformat(),
    }


@router.get("/sectors")
async def get_sector_strength():
    """35 個族群今日強弱排行"""
    from services.twse import fetch_twse_stock_performance
    from routers.scan import TW_THEME_SECTORS, TW_STOCK_LIST, _calc_score
    perf = await fetch_twse_stock_performance()
    if not perf:
        return {"error": "無法取得資料", "sectors": []}
    results = []
    for sector_name, tickers in TW_THEME_SECTORS.items():
        stocks, tvol = [], 0
        for code in tickers:
            if code not in perf:
                continue
            p = perf[code]
            vol = p.get("volume", 0)
            tvol += vol
            stocks.append({
                "ticker": code,
                "name": TW_STOCK_LIST.get(code, code),
                "current_price": p["current_price"],
                "change_pct": p["change_pct"],
                "volume": vol,
                "to_limit_pct": p.get("to_limit_pct"),
                "is_hot": p["change_pct"] >= 7,
            })
        if len(stocks) >= 2:
            results.append(_calc_score(sector_name, stocks, tvol))
    results.sort(key=lambda x: x["strength_score"], reverse=True)
    return {"sectors": results, "updated_at": datetime.utcnow().isoformat()}


@router.get("/margin-health/{user_id}")
async def get_margin_health(user_id: str):
    """個人融資維持率即時計算"""
    items = []
    async for h in db.holdings.find({"user_id": user_id, "margin": True}):
        current     = h.get("current_price") or h["entry_price"]
        shares_real = h.get("shares", 0) * (1000 if h.get("unit") == "lot" else 1)
        if shares_real <= 0:
            continue
        market_value = current * shares_real
        cost         = h["entry_price"] * shares_real
        loan         = cost * h.get("margin_ratio", 0.6)
        if loan <= 0:
            continue
        maintenance = round(market_value / loan * 100, 1)
        # 追繳線 130%：price × shares = loan × 1.3 → price = loan×1.3/shares
        call_price  = round((loan * 1.3) / shares_real, 2)
        items.append({
            "ticker":          h["ticker"],
            "name":            h.get("name", h["ticker"]),
            "current":         current,
            "market_value":    round(market_value, 0),
            "loan":            round(loan, 0),
            "maintenance_pct": maintenance,
            "call_price":      call_price,
            "status":          "danger" if maintenance < 130 else "warning" if maintenance < 160 else "safe",
        })
    items.sort(key=lambda x: x["maintenance_pct"])
    return {"holdings": items, "checked_at": datetime.utcnow().isoformat()}


@router.get("/holdings-divergence/{user_id}")
async def get_holdings_divergence(user_id: str):
    """持倉個股背離偵測"""
    from services.finmind import get_tw_technical
    from services.yfinance_service import calculate_technical_indicators
    results = []
    async for h in db.holdings.find({"user_id": user_id}):
        ticker = h["ticker"]
        market = h.get("market", "tw")
        if market == "tw":
            tech = await get_tw_technical(ticker)
        else:
            tech = await asyncio.to_thread(calculate_technical_indicators, ticker, market)
        if not tech:
            continue
        rsi  = tech.get("rsi", 50)
        kd   = tech.get("kd", {})
        div  = {"type": "none", "signal": None}
        if kd.get("overbought") and rsi < 65:
            div = {"type": "bearish", "signal": "KD 超買但 RSI 動能不足，頂背離風險"}
        elif kd.get("oversold") and rsi > 35:
            div = {"type": "bullish", "signal": "KD 超賣但 RSI 回升，底背離反彈機會"}
        results.append({
            "ticker":     ticker,
            "name":       h.get("name", ticker),
            "current":    tech["current_price"],
            "rsi":        rsi,
            "kd":         kd,
            "direction":  tech.get("direction", "中性"),
            "divergence": div,
        })
    return {"stocks": results, "updated_at": datetime.utcnow().isoformat()}

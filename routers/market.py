import asyncio
from fastapi import APIRouter
from datetime import datetime
import httpx
from database import db

router = APIRouter(prefix="/market", tags=["market"])

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
    # 加權指數
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

    # 台指期
    tw_futures = None
    try:
        from services.finmind import get_tw_futures_data
        tw_futures = await get_tw_futures_data()
    except Exception as e:
        print(f"[market] futures: {e}")

    # 漲跌家數 from TWSE（重新抓，不用 cache）
    breadth = None
    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        if data.get("stat") == "OK":
            items = data.get("data", [])
            up   = sum(1 for x in items if _safe_float(x[3]) > _safe_float(x[2]))
            down = sum(1 for x in items if _safe_float(x[3]) < _safe_float(x[2]))
            flat = len(items) - up - down
            ratio = round(up / down, 2) if down > 0 else 99
            # 計算漲停跌停（漲幅 >= 9.9%）
            limit_up   = sum(1 for x in items if _safe_pct(x) >= 9.9)
            limit_down = sum(1 for x in items if _safe_pct(x) <= -9.9)
            breadth = {
                "up": up, "down": down, "flat": flat,
                "limit_up": limit_up, "limit_down": limit_down,
                "ratio": ratio,
                "breadth": "強勢" if ratio > 2 else "弱勢" if ratio < 0.5 else "平衡",
            }
    except Exception as e:
        print(f"[market] breadth: {e}")

    # 融資餘額（用 TWSE MI_MARGN）
    margin = None
    try:
        url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=MS"
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        rows = data.get("data", [])
        if rows:
            latest = rows[-1]
            bal = _safe_float(latest[1]) if len(latest) > 1 else 0
            prev_bal = _safe_float(rows[-2][1]) if len(rows) > 1 and len(rows[-2]) > 1 else bal
            chg = round((bal - prev_bal) / prev_bal * 100, 2) if prev_bal else 0
            margin = {
                "balance": round(bal / 1e8, 2),
                "change_pct": chg,
                "trend": "增加" if chg > 0 else "減少",
            }
    except Exception as e:
        print(f"[market] margin: {e}")

    return {"tw_index": tw_index, "tw_futures": tw_futures,
            "breadth": breadth, "margin": margin,
            "updated_at": datetime.utcnow().isoformat()}

def _safe_float(s):
    try:
        return float(str(s).replace(",", ""))
    except:
        return 0

def _safe_pct(row):
    try:
        close = _safe_float(row[3])
        prev  = close - _safe_float(row[4])
        return round((close - prev) / prev * 100, 2) if prev > 0 else 0
    except:
        return 0

@router.get("/institutional")
async def get_institutional():
    """三大法人今日買賣超（直接用 TWSE T86）"""
    try:
        url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL"
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        rows = data.get("data", [])
        if not rows:
            return {"error": "今日無資料（盤中或假日）"}

        # T86 格式：[股票代號, 名稱, 外資買, 外資賣, 外資淨, 投信買, 投信賣, 投信淨, 自營商買, 自營商賣, 自營商淨, ...]
        foreign_net = sum(_safe_float(r[4]) for r in rows if len(r) > 4)
        trust_net   = sum(_safe_float(r[7]) for r in rows if len(r) > 7)
        dealer_net  = sum(_safe_float(r[10]) for r in rows if len(r) > 10)
        date_str    = data.get("date", datetime.now().strftime("%Y%m%d"))

        return {
            "date":          date_str,
            "foreign_net":   foreign_net,
            "foreign_trend": "買超" if foreign_net > 0 else "賣超",
            "trust_net":     trust_net,
            "trust_trend":   "買超" if trust_net > 0 else "賣超",
            "dealer_net":    dealer_net,
            "dealer_trend":  "買超" if dealer_net > 0 else "賣超",
        }
    except Exception as e:
        print(f"[market] institutional: {e}")
        return {"error": str(e)}

@router.get("/margin-trend")
async def get_margin_trend():
    """融資餘額近期趨勢（TWSE MI_MARGN）"""
    try:
        url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=MS"
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        rows = data.get("data", [])
        if not rows:
            return {"error": "無資料"}

        history = []
        for i, row in enumerate(rows[-20:]):
            bal = _safe_float(row[1]) if len(row) > 1 else 0
            prev_bal = _safe_float(rows[max(0,i-1)][1]) if i > 0 else bal
            chg = round((bal - prev_bal) / prev_bal * 100, 2) if prev_bal else 0
            history.append({
                "date":       str(row[0]) if row else "",
                "balance":    round(bal / 1e8, 2),
                "change_pct": chg,
            })

        latest = history[-1]
        return {
            "balance":    latest["balance"],
            "change_pct": latest["change_pct"],
            "trend":      "增加" if latest["change_pct"] > 0 else "減少",
            "history":    history,
        }
    except Exception as e:
        print(f"[market] margin-trend: {e}")
        return {"error": str(e)}

@router.get("/sectors")
async def get_sector_strength():
    """族群強弱（直接從 TWSE 抓，不用 cache）"""
    try:
        from routers.scan import TW_THEME_SECTORS, TW_STOCK_LIST, _calc_score
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
        async with httpx.AsyncClient(timeout=20, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        if data.get("stat") != "OK":
            return {"error": "TWSE 無資料", "sectors": []}

        fields = data.get("fields", [])
        close_idx  = next((i for i, f in enumerate(fields) if "收盤" in str(f)), None)
        change_idx = next((i for i, f in enumerate(fields) if "漲跌" in str(f)), None)
        vol_idx    = next((i for i, f in enumerate(fields) if "成交股數" in str(f)), None)

        perf = {}
        for item in data.get("data", []):
            try:
                code = str(item[0]).strip()
                if not (code.isdigit() and 4 <= len(code) <= 5):
                    continue
                close = _safe_float(item[close_idx]) if close_idx is not None else 0
                chg   = _safe_float(item[change_idx]) if change_idx is not None else 0
                vol   = _safe_float(item[vol_idx]) if vol_idx is not None else 0
                prev  = close - chg
                chg_pct = round(chg / prev * 100, 2) if prev > 0 else 0
                perf[code] = {"current_price": close, "change_pct": chg_pct, "volume": vol}
            except:
                continue

        if not perf:
            return {"error": "TWSE 資料解析失敗", "sectors": []}

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
                               "volume": vol, "to_limit_pct": None, "is_hot": p["change_pct"] >= 7})
            if len(stocks) >= 2:
                results.append(_calc_score(sector_name, stocks, tvol))
        results.sort(key=lambda x: x["strength_score"], reverse=True)
        return {"sectors": results}
    except Exception as e:
        print(f"[market] sectors: {e}")
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

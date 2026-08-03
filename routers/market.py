import os
import asyncio
from fastapi import APIRouter
from datetime import datetime, timedelta, timezone
import httpx
from database import db

router = APIRouter(prefix="/market", tags=["market"])

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"

INTL_INDICES = {
    "taiex":  {"ticker": "^TWII",    "name": "台股加權"},
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
        try:
            bar = hist.index[-1]
            bar = bar.tz_convert("Asia/Taipei") if bar.tzinfo else bar
            bar_date = bar.strftime("%m/%d")
        except Exception:
            bar_date = ""
        return {
            "current": cur, "change_pct": chg, "rsi": rsi,
            "kd": {"k": round(float(k.iloc[-1]), 1), "d": round(float(d.iloc[-1]), 1)},
            "closes": closes.tolist()[-60:],
            "bar_date": bar_date,
        }
    except Exception as e:
        print(f"[yf] {ticker}: {e}")
        return None

def _divergence(closes: list, rsi: float, kd_k: float, name: str = "") -> dict:
    """背離偵測（詳細版）：
    頂背離 = 價格進入近60日高點區，但 RSI 未同步走高（動能不足）
    底背離 = 價格接近近60日低點區，但 RSI 未再破低（賣壓竭盡）
    """
    if len(closes) < 20:
        return {"type": "none", "signal": None}
    peak    = max(closes)
    trough  = min(closes)
    current = closes[-1]
    dist_peak   = round((current - peak) / peak * 100, 1)
    dist_trough = round((current - trough) / trough * 100, 1)

    if current >= peak * 0.97 and rsi < 65:
        return {
            "type": "bearish",
            "signal": (
                f"現價 {current:,.2f} 已進入近60日高點區（距高點 {dist_peak:+.1f}%），"
                f"但 RSI 僅 {rsi}、K值 {kd_k} 未同步走高 → 價格創高、動能未跟上，"
                f"為典型頂背離。依你的規則「指數創高但指標未創高＝下」屬偏空警訊，"
                f"建議：檢視持股停利位置、此區不追高，若跌破短均線考慮減碼"
            ),
        }
    if current <= trough * 1.03 and rsi > 35:
        return {
            "type": "bullish",
            "signal": (
                f"現價 {current:,.2f} 接近近60日低點區（距低點 {dist_trough:+.1f}%），"
                f"但 RSI {rsi} 未再破低 → 賣壓竭盡的底背離。"
                f"依你的規則「指數創低但指標未破低＝上」屬偏多訊號，"
                f"可留意止跌K棒確認後的反彈進場機會"
            ),
        }
    return {"type": "none", "signal": None}

# ── FinMind 整體市場（Total 資料集）──────────────────────────────

async def _fm_total(dataset: str, days: int = 30) -> list:
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end   = datetime.now().strftime("%Y-%m-%d")
    params = {"dataset": dataset, "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(FINMIND_BASE, params=params)
    j = r.json()
    if j.get("status") != 200:
        raise Exception(f"FinMind {dataset}: {j.get('msg', 'unknown')}")
    return j.get("data", [])

def _tw_last_trading_label() -> str:
    """推算台股最近交易日（14:00 前視為前一交易日收盤資料）"""
    now = datetime.now(timezone(timedelta(hours=8)))
    d = now
    if d.weekday() >= 5 or (now.hour * 60 + now.minute) < 14 * 60:
        d = d - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d.strftime("%m/%d") + " 收盤"

def _to_yi(raw: float) -> float:
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
            "history": history, "data_date": latest["date"]}

# ── TWSE OpenAPI（STOCK_DAY_ALL 替代方案）────────────────────────

_openapi_cache = {"date": None, "data": {}}

async def _twse_openapi_perf() -> dict:
    """全市場收盤資料 via openapi.twse.com.tw（官方開放資料，無防爬）"""
    global _openapi_cache
    today = datetime.now().strftime("%Y%m%d")
    if _openapi_cache["date"] == today and _openapi_cache["data"]:
        return _openapi_cache["data"]

    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    async with httpx.AsyncClient(timeout=25, verify=False) as c:
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0",
                                      "accept": "application/json"})
    data = r.json()  # list of dicts
    perf = {}
    for it in data:
        code = str(it.get("Code", "")).strip()
        if not (code.isdigit() and 4 <= len(code) <= 5):
            continue
        close = _safe_float(it.get("ClosingPrice"))
        chg   = _safe_float(it.get("Change"))
        vol   = _safe_float(it.get("TradeVolume"))
        prev  = close - chg
        if close <= 0 or prev <= 0:
            continue
        chg_pct = round(chg / prev * 100, 2)
        limit_price  = round(prev * 1.10, 2)
        to_limit_pct = round((limit_price - close) / close * 100, 2)
        perf[code] = {"name": it.get("Name", ""), "current_price": close,
                      "change_pct": chg_pct, "volume": vol,
                      "to_limit_pct": to_limit_pct}
    if perf:
        _openapi_cache.update({"date": today, "data": perf})
    return perf

async def _get_perf() -> dict:
    """收盤資料：OpenAPI 為主，舊 rwd 端點備援"""
    try:
        perf = await _twse_openapi_perf()
        if perf:
            return perf
    except Exception as e:
        print(f"[market] openapi perf: {e}")
    try:
        from services.twse import fetch_twse_stock_performance
        return await fetch_twse_stock_performance() or {}
    except Exception as e:
        print(f"[market] rwd perf: {e}")
        return {}

# ── Endpoints ────────────────────────────────────────────────────

@router.get("/indices")
async def get_indices():
    keys  = list(INTL_INDICES.keys())
    datas = await asyncio.gather(
        *[asyncio.to_thread(_yf_data, INTL_INDICES[k]["ticker"]) for k in keys]
    )
    results = {}
    for key, data in zip(keys, datas):
        if not data:
            continue
        div = _divergence(data["closes"], data["rsi"], data["kd"]["k"],
                          INTL_INDICES[key]["name"])
        vix_status = None
        if key == "vix":
            v = data["current"]
            vix_status = "danger" if v >= 30 else "warning" if v >= 20 else "normal"
        results[key] = {
            "name": INTL_INDICES[key]["name"], "current": data["current"],
            "change_pct": data["change_pct"], "rsi": data["rsi"],
            "kd": data["kd"], "divergence": div, "vix_status": vix_status,
            "data_date": data.get("bar_date", ""),
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
                        "rsi": data["rsi"], "kd": data["kd"],
                        "data_date": data.get("bar_date", "")}
            # Sponsor 即時：snapshot 001 覆蓋盤中點位
            try:
                async with httpx.AsyncClient(timeout=5) as _c:
                    _r = await _c.get(FINMIND_BASE, params={
                        "dataset": "taiwan_stock_tick_snapshot",
                        "data_id": "001", "token": FINMIND_TOKEN})
                _j = _r.json()
                if _j.get("status") == 200 and _j.get("data"):
                    _it = _j["data"][-1]
                    _px = float(_it.get("close", 0) or 0)
                    if _px > 0:
                        prev = data["closes"][-2] if len(data["closes"]) >= 2 else _px
                        tw_index["current"] = round(_px, 0)
                        tw_index["change_pct"] = round((_px - prev) / prev * 100, 2) if prev else 0
                        _dt = str(_it.get("date", ""))
                        tw_index["data_date"] = (_dt[5:16].replace("-", "/") if len(_dt) >= 16 else _dt) + " 即時"
            except Exception:
                pass
    except Exception as e:
        print(f"[market] TWII: {e}")

    tw_futures = None
    try:
        from services.finmind import get_tw_futures_data
        tw_futures = await get_tw_futures_data()
    except Exception as e:
        print(f"[market] futures: {e}")

    breadth = None
    try:
        perf = await _get_perf()
        if perf:
            up   = sum(1 for v in perf.values() if v["change_pct"] > 0)
            down = sum(1 for v in perf.values() if v["change_pct"] < 0)
            flat = len(perf) - up - down
            limit_up   = sum(1 for v in perf.values() if v.get("to_limit_pct") is not None and v.get("to_limit_pct") <= 0.1)
            limit_down = sum(1 for v in perf.values() if v["change_pct"] <= -9.5)
            ratio = round(up / down, 2) if down > 0 else 99
            breadth = {"up": up, "down": down, "flat": flat,
                       "limit_up": limit_up, "limit_down": limit_down, "ratio": ratio,
                       "breadth": "強勢" if ratio > 2 else "弱勢" if ratio < 0.5 else "平衡",
                       "data_date": _tw_last_trading_label()}
    except Exception as e:
        print(f"[market] breadth: {e}")

    margin = None
    try:
        mt = await _margin_trend_data()
        if mt:
            margin = {"balance": mt["balance"], "change_pct": mt["change_pct"], "trend": mt["trend"],
                      "data_date": mt["history"][-1]["date"] if mt.get("history") else ""}
    except Exception as e:
        print(f"[market] margin: {e}")

    return {"tw_index": tw_index, "tw_futures": tw_futures,
            "breadth": breadth, "margin": margin,
            "updated_at": datetime.utcnow().isoformat()}

@router.get("/institutional")
async def get_institutional():
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
        from routers.scan import TW_THEME_SECTORS, TW_STOCK_LIST, _calc_score
        perf = await _get_perf()
        if not perf:
            return {"error": "收盤資料取得失敗", "sectors": []}
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
        return {"sectors": results, "data_date": _tw_last_trading_label()}
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
        k_val = kd.get("k", 50)
        div = {"type": "none", "signal": None}
        if kd.get("overbought") and rsi < 65:
            div = {"type": "bearish",
                   "signal": f"K值 {k_val} 已達超買區（>80）但 RSI 僅 {rsi} 未同步衝高 → 價格衝高、動能未跟上的頂背離，留意回檔壓力、檢視停利位"}
        elif kd.get("oversold") and rsi > 35:
            div = {"type": "bullish",
                   "signal": f"K值 {k_val} 已進超賣區（<20）但 RSI {rsi} 未再破低 → 賣壓竭盡的底背離，可留意止跌反彈的加碼時機"}
        results.append({"ticker": ticker, "name": h.get("name", ticker),
                        "current": tech["current_price"], "rsi": rsi, "kd": kd,
                        "direction": tech.get("direction", "中性"), "divergence": div})
    return {"stocks": results}


# ── 反轉儀表板資料源（FinMind 已查證資料集）─────────────────────

async def _fm_q(dataset: str, days: int = 14, data_id: str = None) -> list:
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end   = datetime.now().strftime("%Y-%m-%d")
    params = {"dataset": dataset, "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
    if data_id:
        params["data_id"] = data_id
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(FINMIND_BASE, params=params)
    j = r.json()
    if j.get("status") != 200:
        raise Exception(f"FinMind {dataset}: {j.get('msg', 'unknown')}")
    return j.get("data", [])

async def _futures_oi_data() -> dict:
    """外資台指期淨未平倉 + 散戶小台淨部位（= -三大法人小台淨OI）"""
    rows = await _fm_q("TaiwanFuturesInstitutionalInvestors", days=16, data_id="TX")
    by_date = {}
    for r in rows:
        inst = str(r.get("institutional_investors", "")).lower()
        if "foreign" in inst and "dealer" not in inst:
            net = float(r.get("long_open_interest_balance_volume", 0) or 0)                 - float(r.get("short_open_interest_balance_volume", 0) or 0)
            by_date[r["date"]] = net
    dates = sorted(by_date)
    if not dates:
        raise Exception("TX 外資 OI 無資料")
    latest = by_date[dates[-1]]
    prev5  = by_date[dates[-6]] if len(dates) >= 6 else by_date[dates[0]]
    # 小台散戶 = -(三大法人淨OI)
    retail = None
    try:
        mrows = await _fm_q("TaiwanFuturesInstitutionalInvestors", days=6, data_id="MTX")
        md = {}
        for r in mrows:
            net = float(r.get("long_open_interest_balance_volume", 0) or 0)                 - float(r.get("short_open_interest_balance_volume", 0) or 0)
            md[r["date"]] = md.get(r["date"], 0) + net
        if md:
            retail = -md[sorted(md)[-1]]
    except Exception:
        pass
    return {"date": dates[-1], "foreign_net_oi": int(latest),
            "chg5": int(latest - prev5),
            "trend": "偏多" if latest > 0 else "偏空",
            "warn": bool(latest < 0 and (latest - prev5) < -3000),
            "retail_mtx_net": int(retail) if retail is not None else None}

async def _internals_data() -> dict:
    out = {}
    try:
        from services.signals import pct_above_ma20, adl_status
        out["above_ma20"] = await pct_above_ma20()
        out["adl"] = await adl_status()
    except Exception as e:
        out["internals_error"] = str(e)
    try:
        rows = await _fm_q("TaiwanOptionVix", days=10)
        if rows:
            last = sorted(rows, key=lambda x: (x["date"], x.get("time", "")))[-1]
            out["twvix"] = {"value": round(float(last["vix"]), 2), "date": last["date"]}
    except Exception as e:
        out["twvix"] = {"error": str(e)}
    try:
        rows = await _fm_q("CnnFearGreedIndex", days=10)
        if rows:
            last = sorted(rows, key=lambda x: x["date"])[-1]
            out["fear_greed"] = {"value": round(float(last["fear_greed"]), 0),
                                 "emotion": last.get("fear_greed_emotion", ""), "date": last["date"]}
    except Exception as e:
        out["fear_greed"] = {"error": str(e)}
    try:
        rows = await _fm_q("TaiwanTotalExchangeMarginMaintenance", days=20)
        if rows:
            last = sorted(rows, key=lambda x: x["date"])[-1]
            out["maintenance"] = {"value": round(float(last["TotalExchangeMarginMaintenance"]), 1),
                                  "date": last["date"]}
    except Exception as e:
        out["maintenance"] = {"error": str(e)}
    try:
        y10 = await _fm_q("GovernmentBondsYield", days=14, data_id="United States 10-Year")
        y3m = await _fm_q("GovernmentBondsYield", days=14, data_id="United States 3-Month")
        if y10 and y3m:
            v10 = float(sorted(y10, key=lambda x: x["date"])[-1]["value"])
            v3  = float(sorted(y3m, key=lambda x: x["date"])[-1]["value"])
            sp = round(v10 - v3, 2)
            out["yield_spread"] = {"us10y": v10, "us3m": v3, "spread": sp,
                                   "inverted": sp < 0}
    except Exception as e:
        out["yield_spread"] = {"error": str(e)}
    return out

async def compute_market_signal() -> dict:
    """市場燈號 0-100：融資操作者的每日曝險決策"""
    comps = []
    async def _safe(coro):
        try:
            return await coro
        except Exception:
            return None

    twii_t = asyncio.to_thread(_yf_data, "^TWII")
    vix_t  = asyncio.to_thread(_yf_data, "^VIX")
    twii, vix, perf, inst, foi, mt, internals = await asyncio.gather(
        twii_t, vix_t, _safe(_get_perf()), _safe(get_institutional()),
        _safe(_futures_oi_data()), _safe(_margin_trend_data()), _safe(_internals_data()))

    # 1 趨勢 25
    s = 50; d = "資料不足"
    if twii:
        closes = twii["closes"]
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else ma20
        cur = closes[-1]
        if ma20 and ma60:
            if cur > ma20 and cur > ma60 and ma20 > ma60:
                s, d = 95, f"指數站上月線與季線且多頭排列（{cur:,.0f}）"
            elif cur > ma20 and cur > ma60:
                s, d = 75, "指數站上月線與季線"
            elif cur > ma60:
                s, d = 45, "跌破月線但守住季線"
            else:
                s, d = 15, "跌破月線與季線，空方結構"
    comps.append({"name": "指數趨勢", "weight": 25, "score": s, "detail": d})

    # 2 市場廣度 15
    s = 50; d = "無資料"
    if perf:
        up = sum(1 for v in perf.values() if v["change_pct"] > 0)
        down = sum(1 for v in perf.values() if v["change_pct"] < 0)
        ratio = round(up / down, 2) if down else 9.9
        am = (internals or {}).get("above_ma20", {}) if internals else {}
        pct = am.get("pct")
        base = 90 if ratio >= 2 else 70 if ratio >= 1.2 else 50 if ratio >= 0.8 else 30 if ratio >= 0.5 else 10
        if pct is not None:
            pb = 20 if pct < 20 else 85 if 20 <= pct <= 75 else 35
            s = round((base + pb) / 2)
            d = f"漲跌比 {ratio}，{pct}% 個股站上月線"
        else:
            s, d = base, f"漲跌比 {ratio}"
    comps.append({"name": "市場廣度", "weight": 15, "score": s, "detail": d})

    # 3 VIX 10
    s = 60; d = "無資料"
    if vix:
        v = vix["current"]
        s = 90 if v < 16 else 70 if v < 20 else 40 if v < 25 else 20 if v < 30 else 5
        d = f"VIX {v}"
    comps.append({"name": "恐慌指標", "weight": 10, "score": s, "detail": d})

    # 4 外資現貨 10
    s = 50; d = "無資料"
    if inst and inst.get("foreign_net") is not None:
        f = inst["foreign_net"]
        s = 85 if f > 0 else 25
        d = f"外資現貨{inst.get('foreign_trend','')} {round(abs(f)/1e8,1)}億"
    comps.append({"name": "外資現貨", "weight": 10, "score": s, "detail": d})

    # 5 外資期貨OI 15
    s = 50; d = "無資料"
    if foi:
        fo, ch = foi["foreign_net_oi"], foi["chg5"]
        if fo > 0 and ch >= 0:
            s, d = 90, f"外資期貨淨多 {fo:,} 口且增加"
        elif fo > 0:
            s, d = 65, f"外資期貨淨多 {fo:,} 口但5日減 {abs(ch):,}"
        elif ch < -3000:
            s, d = 10, f"外資期貨淨空 {abs(fo):,} 口且5日大增空單（反轉警訊）"
        else:
            s, d = 30, f"外資期貨淨空 {abs(fo):,} 口"
    comps.append({"name": "外資期貨籌碼", "weight": 15, "score": s, "detail": d})

    # 6 融資動能 10
    s = 60; d = "無資料"
    if mt and mt.get("history"):
        h = mt["history"]
        g5 = round(sum(x["change_pct"] for x in h[-5:]), 2)
        idx5 = 0
        if twii and len(twii["closes"]) >= 6:
            idx5 = round((twii["closes"][-1] / twii["closes"][-6] - 1) * 100, 2)
        if g5 > 3 and idx5 < 1:
            s, d = 10, f"融資5日暴增 {g5}% 但指數僅 {idx5}%（散戶過熱警訊）"
        elif g5 > 1.5:
            s, d = 45, f"融資5日增 {g5}%"
        elif g5 < -1:
            s, d = 85, f"融資5日減 {abs(g5)}%（籌碼沉澱）"
        else:
            s, d = 70, f"融資5日 {g5:+}% 平穩"
    comps.append({"name": "融資動能", "weight": 10, "score": s, "detail": d})

    # 7 大盤維持率 10
    s = 60; d = "無資料"
    mv = (internals or {}).get("maintenance", {}) if internals else {}
    if mv.get("value"):
        v = mv["value"]
        s = 85 if v >= 170 else 65 if v >= 160 else 45 if v >= 150 else 25 if v >= 140 else 5
        d = f"大盤融資維持率 {v}%"
    comps.append({"name": "融資維持率", "weight": 10, "score": s, "detail": d})

    # 8 背離 5
    s = 75; d = "無明顯背離"
    if twii:
        div = _divergence(twii["closes"], twii["rsi"], twii["kd"]["k"], "台股")
        if div["type"] == "bearish":
            s, d = 25, "加權指數頂背離"
        elif div["type"] == "bullish":
            s, d = 90, "加權指數底背離（反彈訊號）"
    adl = (internals or {}).get("adl", {}) if internals else {}
    if adl.get("divergence"):
        s = min(s, 20)
        d += "；ADL 頂背離"
    comps.append({"name": "背離偵測", "weight": 5, "score": s, "detail": d})

    total = round(sum(c["score"] * c["weight"] for c in comps) / sum(c["weight"] for c in comps))
    if total >= 70:
        light, advice = "green", "多頭環境：可維持既有槓桿，照移動停利規則操作，新倉分批"
    elif total >= 45:
        light, advice = "yellow", "震盪轉弱：停利點上移、暫停加碼融資部位，維持率目標 >180%"
    else:
        light, advice = "red", "空方環境：降槓桿優先，減碼至維持率 >200%，等燈號回黃再進場"
    return {"score": total, "light": light, "advice": advice, "components": comps,
            "extras": {"futures_oi": foi, "internals": internals},
            "updated_at": datetime.utcnow().isoformat()}

@router.get("/futures-oi")
async def futures_oi():
    try:
        return await _futures_oi_data()
    except Exception as e:
        return {"error": str(e)}

@router.get("/internals")
async def internals():
    return await _internals_data()

@router.get("/signal")
async def market_signal():
    try:
        return await compute_market_signal()
    except Exception as e:
        return {"error": str(e)}

@router.get("/debug")
async def market_debug():
    out = {"finmind_token_set": bool(FINMIND_TOKEN)}

    try:
        d = await asyncio.to_thread(_yf_data, "^TWII")
        out["twii_yfinance"] = {"ok": bool(d), "current": d["current"] if d else None}
    except Exception as e:
        out["twii_yfinance"] = {"ok": False, "error": str(e)}

    try:
        perf = await _twse_openapi_perf()
        out["twse_openapi"] = {"ok": bool(perf), "rows": len(perf)}
    except Exception as e:
        out["twse_openapi"] = {"ok": False, "error": str(e)}

    try:
        from services.twse import fetch_twse_stock_performance
        perf = await fetch_twse_stock_performance()
        out["twse_rwd_old"] = {"ok": bool(perf), "rows": len(perf)}
    except Exception as e:
        out["twse_rwd_old"] = {"ok": False, "error": str(e)}

    try:
        from routers.scan import TW_THEME_SECTORS
        out["scan_import"] = {"ok": True, "sector_count": len(TW_THEME_SECTORS)}
    except Exception as e:
        out["scan_import"] = {"ok": False, "error": str(e)}

    try:
        rows = await _fm_total("TaiwanStockTotalInstitutionalInvestors", days=7)
        out["finmind_institutional"] = {"ok": True, "rows": len(rows)}
    except Exception as e:
        out["finmind_institutional"] = {"ok": False, "error": str(e)}

    try:
        rows = await _fm_total("TaiwanStockTotalMarginPurchaseShortSale", days=7)
        out["finmind_margin"] = {"ok": True, "rows": len(rows)}
    except Exception as e:
        out["finmind_margin"] = {"ok": False, "error": str(e)}

    for key, coro in [("futures_oi", _futures_oi_data())]:
        try:
            r = await coro
            out[key] = {"ok": True, "sample": r}
        except Exception as e:
            out[key] = {"ok": False, "error": str(e)}
    for key, ds in [("twvix", "TaiwanOptionVix"), ("fear_greed", "CnnFearGreedIndex"),
                    ("maintenance", "TaiwanTotalExchangeMarginMaintenance")]:
        try:
            rows = await _fm_q(ds, days=10)
            out[key] = {"ok": bool(rows), "rows": len(rows)}
        except Exception as e:
            out[key] = {"ok": False, "error": str(e)}
    try:
        from services.snapshot import db_status
        out["snapshot_db"] = await db_status()
    except Exception as e:
        out["snapshot_db"] = {"error": str(e)}

    return out

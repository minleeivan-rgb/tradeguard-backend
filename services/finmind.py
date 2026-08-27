import os
import time
import httpx
from datetime import datetime, timedelta

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"

# ── 搜尋快取（TTL 6 小時，避免每次搜尋都拉 2000+ 筆）──
_stock_info_cache: dict = {"data": None, "fetched_at": 0.0}
_STOCK_INFO_TTL = 6 * 3600

async def fm_get(dataset: str, stock_id: str, start_date: str = None, end_date: str = None) -> list:
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    params = {
        "dataset": dataset,
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date,
        "token": FINMIND_TOKEN,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(FINMIND_BASE, params=params)
    data = r.json()
    if data.get("status") != 200:
        raise Exception(f"FinMind error: {data.get('msg', 'unknown')}")
    return data.get("data", [])


_name_map_cache: dict = {"map": None, "fetched_at": 0.0}


async def get_tw_name_map() -> dict:
    """全台股代號→中文名稱對照表（上市/上櫃/興櫃，約 2000+ 檔，6 小時快取）"""
    global _name_map_cache, _stock_info_cache
    now = time.time()
    if _name_map_cache["map"] and (now - _name_map_cache["fetched_at"]) <= _STOCK_INFO_TTL:
        return _name_map_cache["map"]
    try:
        if _stock_info_cache["data"] is None or (now - _stock_info_cache["fetched_at"]) > _STOCK_INFO_TTL:
            params = {"dataset": "TaiwanStockInfo", "token": FINMIND_TOKEN}
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.get(FINMIND_BASE, params=params)
            _stock_info_cache["data"] = r.json().get("data", [])
            _stock_info_cache["fetched_at"] = now
        m = {}
        for s in _stock_info_cache["data"] or []:
            sid = str(s.get("stock_id", "")).strip()
            nm = str(s.get("stock_name", "")).strip()
            if sid and nm and sid not in m:
                m[sid] = nm
        # 併入內建表（內建優先度低，僅補 FinMind 沒有的）
        try:
            from routers.scan import TW_STOCK_LIST
            for k, v in TW_STOCK_LIST.items():
                m.setdefault(k, v)
        except Exception:
            pass
        if m:
            _name_map_cache = {"map": m, "fetched_at": now}
        return m
    except Exception as e:
        print(f"[FinMind] name map error: {e}")
        return _name_map_cache["map"] or {}


async def get_tw_stock_name(ticker: str) -> str | None:
    """由 TaiwanStockInfo 精確取得中文名稱（共用 6 小時快取）"""
    global _stock_info_cache
    try:
        now = time.time()
        if _stock_info_cache["data"] is None or (now - _stock_info_cache["fetched_at"]) > _STOCK_INFO_TTL:
            params = {"dataset": "TaiwanStockInfo", "token": FINMIND_TOKEN}
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(FINMIND_BASE, params=params)
            _stock_info_cache["data"] = r.json().get("data", [])
            _stock_info_cache["fetched_at"] = now
        t = str(ticker).strip()
        for s in _stock_info_cache["data"] or []:
            if str(s.get("stock_id", "")).strip() == t:
                nm = str(s.get("stock_name", "")).strip()
                if nm:
                    return nm
        return None
    except Exception as e:
        print(f"[FinMind] name lookup error: {e}")
        return None


async def search_tw_stock(q: str) -> list:
    """搜尋台股代號或名稱（快取 6 小時）"""
    global _stock_info_cache
    try:
        now = time.time()
        if _stock_info_cache["data"] is None or (now - _stock_info_cache["fetched_at"]) > _STOCK_INFO_TTL:
            params = {"dataset": "TaiwanStockInfo", "token": FINMIND_TOKEN}
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(FINMIND_BASE, params=params)
            raw = r.json()
            _stock_info_cache["data"] = raw.get("data", [])
            _stock_info_cache["fetched_at"] = now

        stocks = _stock_info_cache["data"]
        q = q.strip()
        results = []
        for s in stocks:
            code = s.get("stock_id", "")
            name = s.get("stock_name", "")
            if q in code or q in name:
                results.append({"ticker": code, "name": name, "market": "tw"})
                if len(results) >= 10:
                    break
        return results
    except Exception as e:
        print(f"[FinMind] search error: {e}")
        return []


async def get_tw_stock_price(stock_id: str) -> dict:
    """取得台股收盤價和基本技術指標"""
    try:
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        rows = await fm_get("TaiwanStockPrice", stock_id, start, end)
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        closes = [float(r["close"]) for r in rows]
        current = closes[-1]
        highest = max(closes)
        def ma(n):
            if len(closes) < n:
                return None
            return round(sum(closes[-n:]) / n, 2)
        ma20 = ma(20)
        ma60 = ma(60)
        return {
            "current_price": round(current, 2),
            "highest_price": round(highest, 2),
            "ma20": ma20,
            "ma60": ma60,
            "ma20_diff_pct": round((current - ma20) / ma20 * 100, 2) if ma20 else None,
            "ma60_diff_pct": round((current - ma60) / ma60 * 100, 2) if ma60 else None,
            "raw_closes": closes,
            "raw_rows": rows,
        }
    except Exception as e:
        print(f"[FinMind] price error {stock_id}: {e}")
        return None


async def get_tw_technical(stock_id: str, live_price: float | None = None,
                           use_live: bool = True) -> dict:
    """台股技術指標。
    use_live=True（預設）：盤中把「即時價」當作今日這一個點併入序列後再算均線，
    與券商看盤軟體的顯示方式一致。回測/掃描歷史請傳 use_live=False。"""
    import pandas as pd
    try:
        data = await get_tw_stock_price(stock_id)
        if not data or len(data["raw_closes"]) < 60:
            return None

        rows = data["raw_rows"]
        closes = pd.Series([float(r["close"]) for r in rows])
        highs  = pd.Series([float(r["max"]) for r in rows])
        lows   = pd.Series([float(r["min"]) for r in rows])
        vols   = pd.Series([float(r.get("Trading_Volume", 0)) for r in rows])

        last_date  = str(rows[-1].get("date", ""))
        prev_close = float(closes.iloc[-1])          # 最後一個「已收盤」日的收盤價
        tw_today   = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")

        live = live_price
        if use_live and live is None:
            try:
                from services.yfinance_service import get_tw_realtime_price
                rt = await get_tw_realtime_price(stock_id)
                if rt and rt.get("current_price"):
                    live = float(rt["current_price"])
            except Exception:
                live = None

        ma_basis   = f"{last_date} 收盤"
        price_note = "收盤價"
        if use_live and live and live > 0:
            if last_date == tw_today:
                # 日線已含今日 → 以即時價覆蓋今日這一點
                closes.iloc[-1] = live
                highs.iloc[-1]  = max(float(highs.iloc[-1]), live)
                lows.iloc[-1]   = min(float(lows.iloc[-1]), live)
                prev_close = float(closes.iloc[-2])
            else:
                # 日線只到前一交易日 → 追加今日這一點
                closes = pd.concat([closes, pd.Series([live])], ignore_index=True)
                highs  = pd.concat([highs,  pd.Series([live])], ignore_index=True)
                lows   = pd.concat([lows,   pd.Series([live])], ignore_index=True)
            ma_basis   = f"{last_date} 收盤 + 今日即時價（{live}）"
            price_note = "盤中即時"

        current = float(closes.iloc[-1])

        # MA
        ma5   = round(float(closes.tail(5).mean()), 2)
        ma10  = round(float(closes.tail(10).mean()), 2) if len(closes) >= 10 else None
        ma20  = round(float(closes.tail(20).mean()), 2)
        ma60  = round(float(closes.tail(60).mean()), 2)
        ma120 = round(float(closes.tail(120).mean()), 2) if len(closes) >= 120 else None
        ma240 = round(float(closes.tail(240).mean()), 2) if len(closes) >= 240 else None

        # RSI
        delta = closes.diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi = round(float(100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1]))), 1) if loss.iloc[-1] > 0 else 50.0

        # KD
        low9  = lows.rolling(9).min()
        high9 = highs.rolling(9).max()
        rsv   = (closes - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2).mean()
        d = k.ewm(com=2).mean()
        k_val  = round(float(k.iloc[-1]), 1)
        d_val  = round(float(d.iloc[-1]), 1)
        k_prev = round(float(k.iloc[-2]), 1)
        d_prev = round(float(d.iloc[-2]), 1)
        kd_golden = k_prev < d_prev and k_val > d_val
        kd_death  = k_prev > d_prev and k_val < d_val

        # MACD
        ema12 = closes.ewm(span=12).mean()
        ema26 = closes.ewm(span=26).mean()
        macd_line   = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        histogram   = macd_line - signal_line
        macd_val    = round(float(macd_line.iloc[-1]), 3)
        signal_val  = round(float(signal_line.iloc[-1]), 3)
        hist_val    = round(float(histogram.iloc[-1]), 3)
        hist_prev   = round(float(histogram.iloc[-2]), 3)

        # Bollinger
        bb_mid   = closes.tail(20).mean()
        bb_std   = closes.tail(20).std()
        bb_upper = round(float(bb_mid + 2 * bb_std), 2)
        bb_lower = round(float(bb_mid - 2 * bb_std), 2)
        bb_pct   = round((current - bb_lower) / (bb_upper - bb_lower) * 100, 1) if bb_upper != bb_lower else 50.0

        # Volume
        vol_today = float(vols.iloc[-1])
        vol_ma20  = float(vols.tail(20).mean())
        vol_ratio = round(vol_today / vol_ma20, 2) if vol_ma20 > 0 else 1.0
        vol_basis = f"{last_date} 收盤量"

        # Signals
        bull, bear = [], []
        if kd_golden:    bull.append("KD 黃金交叉")
        if kd_death:     bear.append("KD 死亡交叉")
        if k_val < 20:   bull.append(f"KD 超賣（K={k_val}）")
        if k_val > 80:   bear.append(f"KD 超買（K={k_val}）")
        if rsi < 30:     bull.append(f"RSI 超賣（{rsi}）")
        elif rsi > 70:   bear.append(f"RSI 超買（{rsi}）")
        if macd_val > signal_val:  bull.append("MACD 多頭")
        else:                      bear.append("MACD 空頭")
        if hist_val > hist_prev > 0:  bull.append("MACD 柱狀體擴張")
        if current > ma20:  bull.append("站上月線")
        else:               bear.append("跌破月線")
        if current > ma60:  bull.append("站上季線")
        else:               bear.append("跌破季線")
        if vol_ratio > 1.5 and current > float(closes.iloc[-2]):
            bull.append(f"量增價漲（{vol_ratio}x）")

        direction = "偏多" if len(bull) >= len(bear) + 2 else "偏空" if len(bear) >= len(bull) + 2 else "中性"

        return {
            "ticker": stock_id, "market": "tw",
            "current_price": round(current, 2),
            "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma240": ma240},
            "ma20_diff_pct": round((current - ma20) / ma20 * 100, 2),
            "ma60_diff_pct": round((current - ma60) / ma60 * 100, 2),
            "rsi": rsi,
            "kd": {"k": k_val, "d": d_val, "golden_cross": kd_golden, "death_cross": kd_death,
                   "overbought": k_val > 80, "oversold": k_val < 20},
            "macd": {"macd": macd_val, "signal": signal_val, "histogram": hist_val,
                     "bullish": macd_val > signal_val, "expanding": hist_val > hist_prev > 0},
            "bollinger": {"upper": bb_upper, "mid": round(float(bb_mid), 2), "lower": bb_lower, "pct": bb_pct},
            "volume": {"ratio": vol_ratio, "surge": vol_ratio > 1.5, "basis": vol_basis},
            "bull_signals": bull, "bear_signals": bear, "direction": direction,
            "prev_close": round(prev_close, 2),
            "day_change_pct": round((current - prev_close) / prev_close * 100, 2) if prev_close else 0,
            "last_close_date": last_date,
            "ma_basis": ma_basis,
            "price_basis": price_note,
        }
    except Exception as e:
        print(f"[FinMind] technical error {stock_id}: {e}")
        return None


# ── 大盤市場資料（新增）────────────────────────────────────────────

async def get_tw_index_data(days: int = 120) -> dict | None:
    """台股加權指數日線 + RSI + KD"""
    try:
        import pandas as pd
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanStockIndex", "data_id": "TAIEX",
                  "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FINMIND_BASE, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        closes = pd.Series([float(r["price"]) for r in rows])
        current = float(closes.iloc[-1])
        prev    = float(closes.iloc[-2])
        change_pct = round((current - prev) / prev * 100, 2)
        # RSI
        delta = closes.diff()
        gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi = round(float(100 - (100 / (1 + gain.iloc[-1] / loss.iloc[-1]))), 1) \
              if loss.iloc[-1] > 0 else 50.0
        # KD
        low9  = closes.rolling(9).min()
        high9 = closes.rolling(9).max()
        rsv   = (closes - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2).mean()
        d = k.ewm(com=2).mean()
        return {
            "name": "台股加權指數",
            "current": round(current, 2),
            "change_pct": change_pct,
            "rsi": rsi,
            "kd": {"k": round(float(k.iloc[-1]), 1), "d": round(float(d.iloc[-1]), 1)},
            "date": rows[-1]["date"],
        }
    except Exception as e:
        print(f"[FinMind] TW Index error: {e}")
        return None


async def get_tw_futures_data(days: int = 60) -> dict | None:
    """台指期 TX 日線"""
    try:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanFuturesDaily", "data_id": "TX",
                  "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FINMIND_BASE, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        latest = rows[-1]
        prev   = rows[-2] if len(rows) > 1 else rows[-1]
        close  = float(latest.get("close", 0))
        prev_c = float(prev.get("close", close))
        change_pct = round((close - prev_c) / prev_c * 100, 2) if prev_c else 0
        return {
            "name": "台指期 TX",
            "current": round(close, 0),
            "change_pct": change_pct,
            "date": latest.get("date"),
        }
    except Exception as e:
        print(f"[FinMind] Futures error: {e}")
        return None


async def get_tw_margin_balance() -> dict | None:
    """台股整體融資餘額趨勢"""
    try:
        start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanStockMarginPurchaseShortSale",
                  "data_id": "整體市場",
                  "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FINMIND_BASE, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        latest   = rows[-1]
        prev     = rows[-2] if len(rows) > 1 else rows[-1]
        bal      = float(latest.get("MarginPurchaseBalanceAmount", 0))
        prev_bal = float(prev.get("MarginPurchaseBalanceAmount", bal))
        change_pct = round((bal - prev_bal) / prev_bal * 100, 2) if prev_bal else 0
        return {
            "balance":    round(bal / 1e8, 2),
            "change_pct": change_pct,
            "trend":      "增加" if change_pct > 0 else "減少",
            "date":       latest.get("date"),
        }
    except Exception as e:
        print(f"[FinMind] Margin error: {e}")
        return None


async def get_tw_institutional() -> dict | None:
    """三大法人近期買賣超（外資為主）"""
    try:
        start = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanStockInstitutionalInvestors",
                  "data_id": "整體市場",
                  "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FINMIND_BASE, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        foreign = [r for r in rows if "外" in r.get("name", "")]
        recent  = foreign[-5:] if len(foreign) >= 5 else foreign
        net = sum(float(r.get("buy", 0)) - float(r.get("sell", 0)) for r in recent)
        return {
            "foreign_net_5d": round(net / 1e8, 2),
            "trend":          "買超" if net > 0 else "賣超",
            "days":           len(recent),
            "date":           rows[-1].get("date"),
        }
    except Exception as e:
        print(f"[FinMind] Institutional error: {e}")
        return None


async def get_tw_institutional_detail() -> dict | None:
    """三大法人各別買賣超（外資/投信/自營商）"""
    try:
        start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanStockInstitutionalInvestors",
                  "data_id": "整體市場",
                  "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FINMIND_BASE, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        latest_date = rows[-1]["date"]
        today_rows  = [r for r in rows if r["date"] == latest_date]

        result = {"date": latest_date}
        for row in today_rows:
            name = row.get("name", "")
            net  = float(row.get("buy", 0)) - float(row.get("sell", 0))
            if "外陸資" in name or "外資自" in name or ("外" in name and "自營" not in name and "投信" not in name):
                result["foreign_net"]   = net
                result["foreign_trend"] = "買超" if net > 0 else "賣超"
            elif "投信" in name:
                result["trust_net"]     = net
                result["trust_trend"]   = "買超" if net > 0 else "賣超"
            elif "自營" in name:
                result["dealer_net"]    = net
                result["dealer_trend"]  = "買超" if net > 0 else "賣超"
        return result
    except Exception as e:
        print(f"[FinMind] institutional detail error: {e}")
        return None


async def get_tw_margin_trend(days: int = 20) -> dict | None:
    """融資餘額近期趨勢（含歷史數列）"""
    try:
        start = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanStockMarginPurchaseShortSale",
                  "data_id": "整體市場",
                  "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FINMIND_BASE, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        rows = rows[-days:]  # 取最近 N 天

        history = []
        for i, row in enumerate(rows):
            bal = float(row.get("MarginPurchaseBalanceAmount", 0))
            prev_bal = float(rows[i-1].get("MarginPurchaseBalanceAmount", bal)) if i > 0 else bal
            chg_pct = round((bal - prev_bal) / prev_bal * 100, 2) if prev_bal else 0
            history.append({
                "date": row["date"],
                "balance": round(bal / 1e8, 2),
                "change_pct": chg_pct,
            })

        latest = history[-1]
        return {
            "balance":    latest["balance"],
            "change_pct": latest["change_pct"],
            "trend":      "增加" if latest["change_pct"] > 0 else "減少",
            "history":    history,
        }
    except Exception as e:
        print(f"[FinMind] margin trend error: {e}")
        return None


async def get_tw_futures_data(days: int = 60) -> dict | None:
    """台指期 TX 日線（修正欄位名稱）"""
    try:
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end   = datetime.now().strftime("%Y-%m-%d")
        params = {"dataset": "TaiwanFuturesDaily", "data_id": "TX",
                  "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(FINMIND_BASE, params=params)
        rows = r.json().get("data", [])
        if not rows:
            return None
        rows = sorted(rows, key=lambda x: x["date"])
        latest = rows[-1]
        prev   = rows[-2] if len(rows) > 1 else rows[-1]
        # 嘗試多種欄位名稱
        close_fields = ["close", "Close", "settlement_price", "SettlementPrice", "close_price"]
        close_val  = next((float(latest[f]) for f in close_fields if f in latest and latest[f]), 0)
        prev_val   = next((float(prev[f]) for f in close_fields if f in prev and prev[f]), close_val)
        change_pct = round((close_val - prev_val) / prev_val * 100, 2) if prev_val else 0
        return {"name": "台指期 TX", "current": round(close_val, 0),
                "change_pct": change_pct, "date": latest.get("date")}
    except Exception as e:
        print(f"[FinMind] Futures error: {e}")
        return None

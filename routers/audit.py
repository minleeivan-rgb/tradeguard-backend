"""
全系統對帳：每個資料源用「第二來源交叉比對」或「數學恆等式」驗證
GET /audit/full            → 12 項自動對帳報告（PASS/WARN/FAIL/INFO + 原始數字）
GET /audit/price/{ticker}  → 任一個股即時價三來源對照（拿去跟券商畫面直接比）
"""
import os
import asyncio
from fastapi import APIRouter
from datetime import datetime, timedelta, timezone
import httpx
from database import db

router = APIRouter(prefix="/audit", tags=["audit"])

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FM = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))

def _lastwd(offset: int = 0) -> str:
    d = datetime.now(TW_TZ) - timedelta(days=offset)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

async def _fm(dataset, start, end=None, data_id=None):
    p = {"dataset": dataset, "start_date": start, "token": FINMIND_TOKEN}
    if end: p["end_date"] = end
    if data_id: p["data_id"] = data_id
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(FM, params=p)
    j = r.json()
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        return j["data"]
    raise Exception(str(j)[:200])

# ── 三個獨立即時價來源（各自直連，不經過 app 的合併函數）──
async def _px_snapshot(t):
    """FinMind Sponsor 即時 snapshot（獨立端點，非 dataset 參數）"""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot",
                            params={"data_id": t, "token": FINMIND_TOKEN})
        j = r.json()
        d = j.get("data")
        if isinstance(d, dict):
            d = [d]
        if d:
            it = d[-1]
            px = float(it.get("close", 0) or 0)
            if px > 0:
                return {"price": px, "time": str(it.get("date", "")),
                        "volume_ratio": it.get("volume_ratio")}
        return {"error": str(j)[:160]}
    except Exception as e:
        return {"error": str(e)[:160]}

async def _px_mis(t):
    h = {"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
    for ex in ("tse", "otc"):
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex}_{t}.tw&json=1&delay=0"
            async with httpx.AsyncClient(timeout=6, verify=False) as c:
                r = await c.get(url, headers=h)
            msg = r.json().get("msgArray", [])
            if msg:
                it = msg[0]
                def f(v):
                    try: return float(v)
                    except: return 0.0
                px = f(it.get("z")) or f(it.get("y"))
                if px > 0:
                    return {"price": px, "time": f'{it.get("d","")} {str(it.get("t",""))[:8]}'}
        except Exception:
            continue
    return None

async def _px_yahoo(t):
    for sfx in (".TW", ".TWO"):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}{sfx}?interval=1m&range=1d"
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            m = r.json()["chart"]["result"][0]["meta"]
            if m.get("regularMarketPrice"):
                ts = m.get("regularMarketTime")
                tt = datetime.fromtimestamp(ts, TW_TZ).strftime("%m/%d %H:%M") if ts else ""
                return {"price": float(m["regularMarketPrice"]), "time": tt}
        except Exception:
            continue
    return None

@router.get("/price/{ticker}")
async def price_triangulate(ticker: str):
    """個股即時價三來源對照——拿去跟你券商畫面同時比"""
    a, b, c = await asyncio.gather(_px_snapshot(ticker), _px_mis(ticker), _px_yahoo(ticker))
    px = [x["price"] for x in (a, b, c) if x and x.get("price")]
    spread = round((max(px) - min(px)) / min(px) * 100, 3) if len(px) >= 2 and min(px) > 0 else None
    live = [x["price"] for x in (a, b) if x and x.get("price")]
    live_spread = round(abs(live[0] - live[1]) / min(live) * 100, 3) if len(live) == 2 else None
    if live_spread is not None:
        verdict = "PASS 即時源一致" if live_spread < 0.3 else "WARN 即時源不一致"
    elif len(live) == 1:
        verdict = "INFO 僅單一即時源可用"
    else:
        verdict = "FAIL 無即時源"
    return {"ticker": ticker,
            "finmind_snapshot": a, "twse_mis": b, "yahoo_delayed": c,
            "live_spread_pct": live_spread, "max_spread_pct_incl_yahoo": spread,
            "verdict": verdict,
            "note": "Yahoo 台股約延遲15-20分鐘，僅作備援，不納入即時一致性判定"}

@router.get("/full")
async def audit_full():
    R = []
    def add(name, verdict, detail, values=None):
        R.append({"item": name, "verdict": verdict, "detail": detail, "values": values})

    # 1. 即時價三角驗證
    try:
        tri = await price_triangulate("2330")
        vd = "PASS" if tri["verdict"].startswith("PASS") else ("FAIL" if tri["verdict"].startswith("FAIL") else "WARN")
        add("即時報價（2330 即時源比對）", vd,
            f"{tri['verdict']}；即時源價差 {tri['live_spread_pct']}%（Yahoo延遲不計）", tri)
    except Exception as e:
        add("即時報價", "FAIL", str(e))

    # 2. 加權指數收盤：yfinance vs FinMind 官方 5 秒指數最後一筆
    try:
        import yfinance as yf
        fm_close, d = None, None
        for off in range(0, 6):
            dd = _lastwd(off)
            try:
                rows = await _fm("TaiwanVariousIndicators5Seconds", dd)
            except Exception:
                rows = []
            if rows:
                fm_close, d = float(rows[-1]["TAIEX"]), dd
                break
        h = yf.Ticker("^TWII").history(period="7d")["Close"]
        yf_close = round(float(h.iloc[-1]), 2)
        if fm_close is None:
            add("加權指數（yfinance vs 官方五秒指數）", "INFO",
                f"官方五秒指數近6日無資料（該資料集常僅盤中提供）；yfinance {yf_close}",
                {"yfinance": yf_close})
        else:
            diff = round(abs(yf_close - fm_close) / fm_close * 100, 3)
            add("加權指數（yfinance vs 官方五秒指數）",
                "PASS" if diff < 0.3 else "WARN",
                f"官方 {fm_close} vs yfinance {yf_close}，差 {diff}%",
                {"official": fm_close, "yfinance": yf_close, "date": d})
    except Exception as e:
        add("加權指數收盤", "FAIL", str(e))

    # 3. 個股收盤三方交叉：OpenAPI vs Mongo快照 vs FinMind逐檔
    try:
        from routers.market import _get_perf
        perf = await _get_perf()
        results = []
        last_dates = await db.market_daily.distinct("date")
        mongo_date = sorted(last_dates)[-1] if last_dates else None
        for t in ("2330", "2327", "2317"):
            fmrows = await _fm("TaiwanStockPrice", _lastwd(7), _lastwd(), t)
            fm_close = float(fmrows[-1]["close"]) if fmrows else None
            fm_date  = fmrows[-1]["date"] if fmrows else None
            op_close = perf.get(t, {}).get("current_price")
            mg = await db.market_daily.find_one({"_id": f"{fm_date}_{t}"}) if fm_date else None
            mg_close = mg["close"] if mg else None
            ok = fm_close is not None and op_close == fm_close and (mg_close is None or mg_close == fm_close)
            results.append({"ticker": t, "date": fm_date, "openapi": op_close,
                            "finmind": fm_close, "mongo": mg_close, "match": ok})
        allok = all(x["match"] for x in results)
        add("個股收盤三方交叉（OpenAPI/FinMind/Mongo）", "PASS" if allok else "FAIL",
            "三來源逐檔比對" + ("全部一致" if allok else "有不一致，見values"), results)
    except Exception as e:
        add("個股收盤交叉", "FAIL", str(e))

    # 4. 分點恆等式：Σ買=Σ賣=成交量
    try:
        from routers.chips import branch_verify
        v = await branch_verify("2330", _lastwd(1) if datetime.now(TW_TZ).hour < 21 else _lastwd())
        bv = v.get("buy_over_volume")
        verdict = "PASS" if bv and 0.97 <= bv <= 1.03 else ("WARN" if bv else "FAIL")
        add("分點恆等式（Σ買=Σ賣=成交量）", verdict,
            f"買/量={v.get('buy_over_volume')} 賣/量={v.get('sell_over_volume')}（≈1=完整且單位為股）", v if verdict != "PASS" else
            {k: v[k] for k in ("date", "sum_buy_raw", "official_volume_shares", "buy_over_volume")})
    except Exception as e:
        add("分點恆等式", "FAIL", str(e))

    # 5. 融資餘額內部恆等式：今日=昨日+買-賣-現償，且日鏈相接
    try:
        raw = await _fm("TaiwanStockTotalMarginPurchaseShortSale", _lastwd(20), _lastwd())
        mp = [r for r in raw if r.get("name") == "MarginPurchaseMoney"]
        bydate = {}
        for r in mp:
            bydate.setdefault(r["date"], []).append(r)
        dup_dates = {d: len(v) for d, v in bydate.items() if len(v) > 1}
        KEYS = ("TodayBalance", "YesBalance", "buy", "sell", "Return")
        merged = []
        for d in sorted(bydate):
            rs = bydate[d]
            if len(rs) == 1:
                merged.append({"date": d, **{k: float(rs[0].get(k, 0) or 0) for k in KEYS}})
            else:
                vals = {tuple(float(x.get(k, 0) or 0) for k in KEYS) for x in rs}
                if len(vals) == 1:
                    merged.append({"date": d, **{k: float(rs[0].get(k, 0) or 0) for k in KEYS}})
                else:
                    merged.append({"date": d, **{k: sum(float(x.get(k, 0) or 0) for x in rs) for k in KEYS}})
        m = merged[-6:]
        errs = []
        for r in m:
            lhs = r["TodayBalance"]
            rhs = r["YesBalance"] + r["buy"] - r["sell"] - r["Return"]
            if abs(lhs - rhs) > 1000:
                errs.append({"date": r["date"], "diff": round(lhs - rhs)})
        gaps = []
        for i in range(1, len(m)):
            gap = m[i]["YesBalance"] - m[i-1]["TodayBalance"]
            if abs(gap) > 1000:
                gaps.append({"from": m[i-1]["date"], "to": m[i]["date"],
                             "prev_today": round(m[i-1]["TodayBalance"]),
                             "this_yes": round(m[i]["YesBalance"]),
                             "gap": round(gap)})
        if not errs and not gaps:
            vd, det = "PASS", "公式與日鏈皆相符（已按日合併重複列）"
        elif not errs and gaps:
            vd, det = "INFO", (f"公式全對，但日鏈有{len(gaps)}處落差 — "
                               f"常見於 FinMind 缺漏個別交易日（非數值錯誤），餘額本身可用")
        else:
            vd, det = "FAIL", f"公式誤差{len(errs)}筆"
        add("融資餘額恆等式（今=昨+買-賣-償）", vd, det,
            {"latest_balance_yi": round(m[-1]["TodayBalance"] / 1e8, 1),
             "rows_per_date_gt1": dup_dates, "formula_errors": errs,
             "chain_gaps": gaps, "dates_checked": [x["date"] for x in m]})
    except Exception as e:
        add("融資餘額恆等式", "FAIL", str(e))

    # 6. ADL 鏈：adl差 = 漲家-跌家
    try:
        docs = [d async for d in db.market_breadth.find({}).sort("date", 1)]
        bad = []
        for i in range(1, len(docs)):
            if docs[i]["adl"] - docs[i-1]["adl"] != docs[i]["up"] - docs[i]["down"]:
                bad.append(docs[i]["date"])
        add("ADL 累積鏈", "PASS" if docs and not bad else ("INFO" if not docs else "FAIL"),
            f"{len(docs)}天，斷鏈{len(bad)}筆" if docs else "尚無資料（需先回補）", {"broken": bad[:5]})
    except Exception as e:
        add("ADL 累積鏈", "FAIL", str(e))

    # 7. 三大法人：逐檔硬比對（同一上市股 Mongo 必須等於 T86）+ 原始欄位揭露
    try:
        chip_dates = await db.market_chips.distinct("date")
        if not chip_dates:
            add("外資買賣超逐檔交叉", "INFO", "Mongo 尚無籌碼資料（需先回補）")
        else:
            cd = sorted(chip_dates)[-1]
            url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL"
            async with httpx.AsyncClient(timeout=20, verify=False) as c2:
                r = await c2.get(url, headers={"User-Agent": "Mozilla/5.0"})
            j = r.json()
            def sf(s):
                try: return float(str(s).replace(",", ""))
                except: return 0.0
            t86 = {}
            for x in j.get("data", []):
                if len(x) > 11:
                    t86[str(x[0]).strip()] = {"foreign": sf(x[4]), "trust": sf(x[10])}
            checks, bad = [], 0
            for t in ("2330", "2317", "2327", "2454", "2412"):
                mg = await db.market_chips.find_one({"_id": f"{cd}_{t}"})
                if not mg or t not in t86:
                    continue
                mf, tf = mg.get("foreign_net", 0), t86[t]["foreign"]
                mt, tt = mg.get("trust_net", 0), t86[t]["trust"]
                okf = abs(mf - tf) <= max(1000, abs(tf) * 0.02)
                okt = abs(mt - tt) <= max(1000, abs(tt) * 0.02)
                if not (okf and okt):
                    bad += 1
                checks.append({"ticker": t, "mongo_foreign": mf, "t86_foreign": tf,
                               "mongo_trust": mt, "t86_trust": tt, "match": okf and okt})
            wide_fields = None
            try:
                w = await _fm("TaiwanStockInstitutionalInvestorsBuySellWide", cd)
                if w:
                    wide_fields = list(w[0].keys())
            except Exception:
                pass
            if not checks:
                add("外資買賣超逐檔交叉", "INFO", f"{cd} 無可比對標的（T86 當日可能未更新）")
            else:
                add("外資/投信逐檔交叉（Mongo vs T86 官方）",
                    "PASS" if bad == 0 else "FAIL",
                    f"{cd} 比對{len(checks)}檔，不符{bad}檔（單位:股，容差2%）",
                    {"checks": checks, "wide_dataset_fields": wide_fields})
    except Exception as e:
        add("外資買賣超逐檔交叉", "WARN", str(e))

    # 8. 分K vs 日線：最後一分K收盤/高低 = 日線收盤/高低
    try:
        d = _lastwd() if datetime.now(TW_TZ).hour >= 16 else _lastwd(1)
        kb = await _fm("TaiwanStockKBar", d, None, "2330")
        dl = await _fm("TaiwanStockPrice", d, d, "2330")
        if kb and dl:
            kb.sort(key=lambda x: x.get("minute", ""))
            k_close = float(kb[-1]["close"])
            k_high  = max(float(x["high"]) for x in kb)
            k_low   = min(float(x["low"]) for x in kb if float(x["low"]) > 0)
            d_close = float(dl[-1]["close"]); d_high = float(dl[-1]["max"]); d_low = float(dl[-1]["min"])
            ok = (k_close == d_close and k_high == d_high and k_low == d_low)
            add("分K vs 日線（2330）", "PASS" if ok else "WARN",
                f"分K收{k_close}/高{k_high}/低{k_low} vs 日線收{d_close}/高{d_high}/低{d_low}",
                {"date": d})
        else:
            add("分K vs 日線", "INFO", f"{d} 資料尚未齊")
    except Exception as e:
        add("分K vs 日線", "FAIL", str(e))

    # 9. 主動ETF異動恆等式：今持股-昨持股 = 買-賣
    try:
        for etf in ("00981A", "00980A", "00982A"):
            hold = await _fm("TaiwanStockActiveETFHolding", _lastwd(10), _lastwd(), etf)
            if not hold:
                continue
            ds = sorted({h["date"] for h in hold})
            if len(ds) < 2:
                continue
            d1, d0 = ds[-1], ds[-2]
            cur = {h["component_stock_id"]: float(h.get("shares", 0) or 0) for h in hold if h["date"] == d1}
            prv = {h["component_stock_id"]: float(h.get("shares", 0) or 0) for h in hold if h["date"] == d0}
            chg = await _fm("TaiwanStockActiveETFHoldingChange", d1, d1, etf)
            bad = 0; checked = 0
            for r in chg[:10]:
                cid = r["component_stock_id"]
                expect = float(r.get("buy", 0) or 0) - float(r.get("sell", 0) or 0)
                actual = cur.get(cid, 0) - prv.get(cid, 0)
                checked += 1
                if abs(expect - actual) > 1:
                    bad += 1
            add(f"主動ETF異動恆等式（{etf}）", "PASS" if checked and bad == 0 else ("WARN" if checked else "INFO"),
                f"{d1} 檢查{checked}檔成分，恆等式不符{bad}檔", {"dates": [d0, d1]})
            break
    except Exception as e:
        add("主動ETF異動恆等式", "WARN", str(e))

    # 10. 期貨OI 基本檢查
    try:
        rows = await _fm("TaiwanFuturesInstitutionalInvestors", _lastwd(7), _lastwd(), "TX")
        neg = [r for r in rows if float(r.get("long_open_interest_balance_volume", 0) or 0) < 0
               or float(r.get("short_open_interest_balance_volume", 0) or 0) < 0]
        add("期貨OI基本檢查", "PASS" if rows and not neg else ("FAIL" if neg else "INFO"),
            f"{len(rows)}筆，OI負值{len(neg)}筆；散戶小台=−(三大法人淨OI)為數學恆等式非爬蟲資料")
    except Exception as e:
        add("期貨OI基本檢查", "FAIL", str(e))

    # 11. MA 自我一致（FinMind closes vs yfinance closes 算 MA20）
    try:
        import yfinance as yf
        fmrows = await _fm("TaiwanStockPrice", _lastwd(45), _lastwd(), "2330")
        fmc = [float(r["close"]) for r in fmrows if float(r.get("close", 0) or 0) > 0]
        yfc = yf.Ticker("2330.TW").history(period="3mo")["Close"].tolist()
        ma_fm = round(sum(fmc[-20:]) / 20, 2)
        ma_yf = round(sum(yfc[-20:]) / 20, 2)
        diff = round(abs(ma_fm - ma_yf) / ma_fm * 100, 2)
        add("MA20 雙來源重算（2330）", "PASS" if diff < 0.5 else "WARN",
            f"FinMind {ma_fm} vs yfinance {ma_yf}，差{diff}%（除權息日附近容許小差）")
    except Exception as e:
        add("MA20 雙來源重算", "WARN", str(e))

    # 12. 指標公式聲明
    add("指標公式", "INFO",
        "RSI=Wilder平滑14（與多數台灣券商一致）；KD=9日RSV、1/3平滑（台灣標準）；"
        "不同軟體若用簡單平均RSI會差幾個點，屬定義差異非資料錯誤")

    summary = {"PASS": sum(1 for x in R if x["verdict"] == "PASS"),
               "WARN": sum(1 for x in R if x["verdict"] == "WARN"),
               "FAIL": sum(1 for x in R if x["verdict"] == "FAIL"),
               "INFO": sum(1 for x in R if x["verdict"] == "INFO")}
    return {"summary": summary, "checks": R,
            "generated_at": datetime.now(TW_TZ).isoformat()}

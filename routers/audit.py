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
    async with httpx.AsyncClient(timeout=6) as c:
        r = await c.get(FM, params={"dataset": "taiwan_stock_tick_snapshot",
                                    "data_id": t, "token": FINMIND_TOKEN})
    j = r.json()
    if j.get("status") == 200 and j.get("data"):
        it = j["data"][-1]
        return {"price": float(it.get("close", 0) or 0), "time": str(it.get("date", ""))}
    return None

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
    px = [x["price"] for x in (a, b, c) if x]
    spread = round((max(px) - min(px)) / min(px) * 100, 3) if len(px) >= 2 and min(px) > 0 else None
    return {"ticker": ticker,
            "finmind_snapshot": a, "twse_mis": b, "yahoo": c,
            "max_spread_pct": spread,
            "verdict": "PASS 三來源一致" if spread is not None and spread < 0.5
                       else ("WARN 價差略大（可能盤中跳動或延遲）" if spread is not None else "INFO 來源不足")}

@router.get("/full")
async def audit_full():
    R = []
    def add(name, verdict, detail, values=None):
        R.append({"item": name, "verdict": verdict, "detail": detail, "values": values})

    # 1. 即時價三角驗證
    try:
        tri = await price_triangulate("2330")
        add("即時報價（2330 三來源）", "PASS" if "PASS" in tri["verdict"] else "WARN",
            f"最大價差 {tri['max_spread_pct']}%", tri)
    except Exception as e:
        add("即時報價", "FAIL", str(e))

    # 2. 加權指數收盤：yfinance vs FinMind 官方 5 秒指數最後一筆
    try:
        import yfinance as yf
        d = _lastwd()
        rows = await _fm("TaiwanVariousIndicators5Seconds", d)
        fm_close = float(rows[-1]["TAIEX"]) if rows else None
        h = yf.Ticker("^TWII").history(period="7d")["Close"]
        yf_close = round(float(h.iloc[-1]), 2)
        diff = round(abs(yf_close - fm_close) / fm_close * 100, 3) if fm_close else None
        add("加權指數收盤（yfinance vs 官方）",
            "PASS" if diff is not None and diff < 0.3 else "WARN",
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
        rows = await _fm("TaiwanStockTotalMarginPurchaseShortSale", _lastwd(14), _lastwd())
        m = sorted([r for r in rows if r.get("name") == "MarginPurchaseMoney"], key=lambda x: x["date"])[-5:]
        errs = []
        for r in m:
            lhs = float(r["TodayBalance"])
            rhs = float(r["YesBalance"]) + float(r["buy"]) - float(r["sell"]) - float(r["Return"])
            if abs(lhs - rhs) > 1000:
                errs.append({"date": r["date"], "diff": lhs - rhs})
        chain_ok = all(abs(float(m[i]["YesBalance"]) - float(m[i-1]["TodayBalance"])) < 1000
                       for i in range(1, len(m)))
        add("融資餘額恆等式（今=昨+買-賣-償）", "PASS" if not errs and chain_ok else "FAIL",
            f"5日檢查，公式誤差{len(errs)}筆，日鏈{'相接' if chain_ok else '斷裂'}",
            {"latest_balance_yi": round(float(m[-1]['TodayBalance'])/1e8, 1), "errors": errs})
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

    # 7. 三大法人：Mongo Σ個股(股) vs TWSE T86 Σ(股)
    try:
        chip_dates = await db.market_chips.distinct("date")
        if chip_dates:
            cd = sorted(chip_dates)[-1]
            agg = await db.market_chips.aggregate([
                {"$match": {"date": cd}},
                {"$group": {"_id": None, "f": {"$sum": "$foreign_net"}, "t": {"$sum": "$trust_net"}}}
            ]).to_list(1)
            mongo_f = agg[0]["f"] if agg else 0
            url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL"
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            t86 = r.json().get("data", [])
            def sf(s):
                try: return float(str(s).replace(",", ""))
                except: return 0
            t86_f = sum(sf(x[4]) for x in t86 if len(x) > 4)
            ratio = round(mongo_f / t86_f, 3) if t86_f else None
            add("外資買賣超（FinMind全市場 vs T86上市）", "INFO",
                f"Mongo(上市+上櫃+興櫃) {round(mongo_f/1e8,2)}億股 vs T86(僅上市) {round(t86_f/1e8,2)}億股，"
                f"比值{ratio}（母體不同，方向一致即合理）",
                {"mongo_date": cd, "mongo_foreign_shares": mongo_f, "t86_foreign_shares": t86_f})
        else:
            add("外資買賣超交叉", "INFO", "Mongo 尚無籌碼資料（需先回補）")
    except Exception as e:
        add("外資買賣超交叉", "WARN", str(e))

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

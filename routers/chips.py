"""
Sponsor 籌碼套件
- /chips/branch/{ticker}   分點籌碼（近5日連買分點、今日買賣超Top、外資系標記、集中度）
- /chips/kbar/{ticker}     分K走勢（1分K，收盤後更新）
- /chips/govbank           八大行庫買賣（國安基金代理指標）
- /chips/moneyflow         產業鏈資金流向
資料集：TaiwanStockTradingDailyReportSecIdAgg / TaiwanStockKBar /
        TaiwanstockGovernmentBankBuySell / TaiwanStockIndustryChainMoneyFlow
"""
import os
import re
from fastapi import APIRouter
from datetime import datetime, timedelta, timezone
import httpx

router = APIRouter(prefix="/chips", tags=["chips"])

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FM_DATA   = "https://api.finmindtrade.com/api/v4/data"
FM_SECAGG = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report_secid_agg"
TW_TZ = timezone(timedelta(hours=8))

FOREIGN_KW = ["美林", "摩根", "高盛", "瑞銀", "花旗", "港商", "新加坡商", "法商",
              "美商", "瑞士", "野村", "大和", "麥格理", "匯豐", "巴克萊", "德意志", "瑞信"]

def _is_foreign(name: str) -> bool:
    return any(k in name for k in FOREIGN_KW)

def _parse(j, label: str) -> list:
    """容錯：有 data list 就收，錯誤時吐完整回應內容"""
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        if j.get("status", 200) == 200 or j["data"]:
            return j["data"]
    raise Exception(f"{label} 回應: {str(j)[:260]}")

async def _fm(dataset: str, start: str, end: str = None, data_id: str = None) -> list:
    params = {"dataset": dataset, "start_date": start, "token": FINMIND_TOKEN}
    if end:
        params["end_date"] = end
    if data_id:
        params["data_id"] = data_id
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.get(FM_DATA, params=params)
    try:
        j = r.json()
    except Exception:
        raise Exception(f"{dataset} HTTP {r.status_code}: {r.text[:200]}")
    return _parse(j, dataset)

@router.get("/branch/{ticker}")
async def branch_chips(ticker: str, days: int = 5):
    """分點籌碼：官方單日分點端點，逐日抓近N個交易日彙總"""
    try:
        url = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
        got = []
        d = datetime.now(TW_TZ)
        tries = 0
        async with httpx.AsyncClient(timeout=40) as cli:
            while len(got) < days and tries < days + 9:
                if d.weekday() < 5:
                    ds = d.strftime("%Y-%m-%d")
                    r = await cli.get(url, params={"data_id": ticker, "date": ds,
                                                   "token": FINMIND_TOKEN})
                    try:
                        rows = _parse(r.json(), f"分點 {ds}")
                    except Exception as pe:
                        if not got and tries > 4:
                            return {"error": str(pe)}
                        rows = []
                    if rows:
                        got.append((ds, rows))
                d -= timedelta(days=1)
                tries += 1
        if not got:
            return {"error": "近期無分點資料（每日約 21:00 後更新）"}
        got.sort()

        # 涵蓋率：分點總買進股數 vs 當日成交量（FinMind 盤後漸進更新，未滿70%視為不完整）
        vol_map = {}
        try:
            vs = (datetime.now(TW_TZ) - timedelta(days=16)).strftime("%Y-%m-%d")
            vrows = await _fm("TaiwanStockPrice", vs, datetime.now(TW_TZ).strftime("%Y-%m-%d"), ticker)
            for x in vrows:
                vol_map[x["date"]] = float(x.get("Trading_Volume", 0) or 0)
        except Exception:
            pass

        def _cov(ds, rows):
            tot = sum(float(r.get("buy", 0) or 0) for r in rows)
            v = vol_map.get(ds)
            return (tot / v) if v and v > 0 else None

        partial_note = None
        while len(got) > 1:
            cv = _cov(got[-1][0], got[-1][1])
            if cv is not None and cv < 0.7:
                partial_note = (f"{got[-1][0]} 分點資料僅更新約 {round(cv*100)}%"
                                f"（FinMind 盤後漸進爬取，約 21:00 後完整）— 已改顯示 {got[-2][0]} 完整資料")
                got.pop()
            else:
                break

        dates = [g[0] for g in got]
        latest = dates[-1]
        cov_latest = _cov(latest, got[-1][1])

        agg = {}
        total_buy_today = 0.0
        for ds, rows in got:
            for r in rows:
                key = str(r.get("securities_trader_id", "")) or str(r.get("securities_trader", ""))
                name = r.get("securities_trader", key)
                b = float(r.get("buy", 0) or 0)
                s = float(r.get("sell", 0) or 0)
                price = float(r.get("price", 0) or 0)
                a = agg.setdefault(key, {"name": name, "daily": {}, "cum": 0.0,
                                         "bp_num": 0.0, "bp_den": 0.0})
                net = b - s
                a["daily"][ds] = a["daily"].get(ds, 0) + net
                a["cum"] += net
                if ds == latest:
                    if net > 0:
                        total_buy_today += net
                    if b > 0 and price > 0:
                        a["bp_num"] += price * b
                        a["bp_den"] += b

        today_list, streak_list = [], []
        for key, a in agg.items():
            tn = a["daily"].get(latest, 0)
            pos_days = sum(1 for dd in dates if a["daily"].get(dd, 0) > 0)
            item = {"branch": a["name"], "foreign": _is_foreign(a["name"]),
                    "today_lots": round(tn / 1000, 0),
                    "cum_lots": round(a["cum"] / 1000, 0),
                    "pos_days": pos_days, "days": len(dates),
                    "buy_price": round(a["bp_num"] / a["bp_den"], 2) if a["bp_den"] > 0 else None}
            today_list.append(item)
            if pos_days >= max(3, len(dates) - 1) and a["cum"] > 0:
                streak_list.append(item)

        top_buy  = sorted([x for x in today_list if x["today_lots"] > 0],
                          key=lambda x: -x["today_lots"])[:10]
        top_sell = sorted([x for x in today_list if x["today_lots"] < 0],
                          key=lambda x: x["today_lots"])[:10]
        streak_list.sort(key=lambda x: -x["cum_lots"])

        conc = None
        if total_buy_today > 0:
            top15 = sum(x["today_lots"] for x in top_buy[:15]) * 1000
            conc = round(top15 / total_buy_today * 100, 1)

        return {"ticker": ticker, "name": await _name_of_async(ticker),
                "date": latest, "window_dates": dates,
                "top_buy": top_buy, "top_sell": top_sell,
                "streak_buyers": streak_list[:10],
                "buy_concentration_pct": conc,
                "coverage_pct": round(cov_latest * 100, 1) if cov_latest is not None else None,
                "partial_note": partial_note,
                "note": "連買=近5日中>=4日淨買超；外資系依券商名稱判斷；每日約21:00後更新"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/kbar/{ticker}")
async def kbar(ticker: str, date: str = ""):
    """1分K（每日 15:50 後更新當日）"""
    try:
        if date:
            rows = await _fm("TaiwanStockKBar", date, None, ticker)
            use_date = date
        else:
            d = datetime.now(TW_TZ)
            rows, use_date = [], None
            for _ in range(7):
                if d.weekday() < 5:
                    ds = d.strftime("%Y-%m-%d")
                    rows = await _fm("TaiwanStockKBar", ds, None, ticker)
                    if rows:
                        use_date = ds
                        break
                d -= timedelta(days=1)
        if not rows:
            return {"error": "無分K資料（每日15:50後更新）"}
        rows.sort(key=lambda x: x.get("minute", ""))
        bars = [{"t": r.get("minute", "")[:5],
                 "c": float(r.get("close", 0) or 0),
                 "v": float(r.get("volume", 0) or 0)} for r in rows]
        closes = [b["c"] for b in bars if b["c"] > 0]
        nm = None
        try:
            from services.finmind import get_tw_stock_name
            nm = await get_tw_stock_name(ticker)
        except Exception:
            pass
        return {"ticker": ticker, "name": nm, "date": use_date, "bars": bars,
                "open": closes[0] if closes else None,
                "high": max(closes) if closes else None,
                "low": min(closes) if closes else None,
                "last": closes[-1] if closes else None}
    except Exception as e:
        return {"error": str(e)}


async def _discover_datasets(keywords: list) -> list:
    """故意打錯 dataset 讓 API 吐出完整合法清單，撈出含關鍵字的名稱"""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(FM_DATA, params={"dataset": "___probe___",
                                             "start_date": "2026-01-01",
                                             "token": FINMIND_TOKEN})
        txt = r.text
        names = re.findall(r"'([A-Za-z0-9]+)'", txt)
        hits = []
        for n in names:
            low = n.lower()
            if any(k in low for k in keywords) and n not in hits:
                hits.append(n)
        return hits
    except Exception:
        return []

@router.get("/govbank")
async def govbank():
    """八大行庫買賣（國安基金動向代理）"""
    try:
        start = (datetime.now(TW_TZ) - timedelta(days=16)).strftime("%Y-%m-%d")
        candidates = ["TaiwanStockGovernmentBankBuySell", "TaiwanstockGovernmentBankBuySell"]
        candidates += [n for n in await _discover_datasets(["government", "govbank"])
                       if n not in candidates]
        candidates += [n for n in await _discover_datasets(["bank"])
                       if "buy" in n.lower() and n not in candidates]
        rows, last_err = None, None
        for ds_name in candidates:
            try:
                rows = await _fm(ds_name, start)
                if rows:
                    break
            except Exception as e:
                last_err = e
        if rows is None:
            return {"error": f"八大行庫資料集全部嘗試失敗（試了 {candidates}）：{last_err}"}
        if not rows:
            return {"error": "無八大行庫資料"}
        by_date = {}
        for r in rows:
            d = r["date"]
            net = float(r.get("buy_amount", 0) or 0) - float(r.get("sell_amount", 0) or 0)
            by_date.setdefault(d, {"net": 0.0, "stocks": {}})
            by_date[d]["net"] += net
            sid = str(r.get("stock_id", ""))
            st = by_date[d]["stocks"].setdefault(sid, {"net": 0.0, "banks": set()})
            st["net"] += net
            if r.get("bank_name"):
                st["banks"].add(r["bank_name"])
        dates = sorted(by_date)
        latest = dates[-1]
        trend = [{"date": d, "net_yi": round(by_date[d]["net"] / 1e8, 2)}
                 for d in dates[-7:]]
        stocks = []
        for sid, st in by_date[latest]["stocks"].items():
            stocks.append({"ticker": sid, "net_yi": round(st["net"] / 1e8, 2),
                           "banks": len(st["banks"])})
        top_buy  = sorted([x for x in stocks if x["net_yi"] > 0],
                          key=lambda x: -x["net_yi"])[:10]
        top_sell = sorted([x for x in stocks if x["net_yi"] < 0],
                          key=lambda x: x["net_yi"])[:10]
        streak = 0
        for t in reversed(trend):
            if t["net_yi"] > 0:
                streak += 1
            else:
                break
        return {"date": latest, "today_net_yi": trend[-1]["net_yi"],
                "buy_streak_days": streak, "trend": trend,
                "top_buy": top_buy, "top_sell": top_sell,
                "note": "八大行庫連續買超常見於政策護盤（國安基金）階段"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/moneyflow")
async def moneyflow():
    """產業鏈資金流向（今日 vs 前一日）"""
    try:
        d = datetime.now(TW_TZ)
        found = []
        while len(found) < 2:
            if d.weekday() < 5:
                ds = d.strftime("%Y-%m-%d")
                rows = await _fm("TaiwanStockIndustryChainMoneyFlow", ds)
                if rows:
                    found.append((ds, rows))
            d -= timedelta(days=1)
            if (datetime.now(TW_TZ) - d).days > 12:
                break
        if not found:
            return {"error": "無產業鏈資金流資料"}
        (d_now, rows_now) = found[0]
        prev_map = {}
        if len(found) > 1:
            for r in found[1][1]:
                if r.get("sub_industry", "") == "":
                    prev_map[r["industry"]] = float(r.get("trading_money_pct", 0) or 0)
        chains = []
        for r in rows_now:
            if r.get("sub_industry", "") != "":
                continue
            ind = r["industry"]
            pct = float(r.get("trading_money_pct", 0) or 0)
            chains.append({"industry": ind, "pct": round(pct, 2),
                           "money_yi": round(float(r.get("trading_money", 0) or 0) / 1e8, 0),
                           "delta": round(pct - prev_map[ind], 2) if ind in prev_map else None,
                           "stock_count": r.get("stock_count")})
        chains.sort(key=lambda x: -x["pct"])
        rising = sorted([c for c in chains if c["delta"] is not None],
                        key=lambda x: -x["delta"])[:5]
        return {"date": d_now, "prev_date": found[1][0] if len(found) > 1 else None,
                "chains": chains[:14], "rising": rising,
                "note": "pct=占全市場個股成交金額比重；delta=較前一交易日增減"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/debug")
async def chips_debug():
    """逐一測 Sponsor 資料源，回傳真實回應形狀"""
    out = {}
    now = datetime.now(TW_TZ)

    async def _raw(url, params, use_bearer=False):
        headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if use_bearer else {}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, params=params, headers=headers)
        try:
            j = r.json()
        except Exception:
            return {"http": r.status_code, "body": r.text[:200]}
        info = {"http": r.status_code, "keys": list(j.keys())[:8] if isinstance(j, dict) else type(j).__name__,
                "status_field": j.get("status") if isinstance(j, dict) else None,
                "msg": j.get("msg") if isinstance(j, dict) else None}
        data = j.get("data") if isinstance(j, dict) else None
        if isinstance(data, list):
            info["rows"] = len(data)
            if data:
                info["sample_keys"] = list(data[0].keys())[:12]
        else:
            info["body_snippet"] = str(j)[:200]
        return info

    start5 = (now - timedelta(days=9)).strftime("%Y-%m-%d")
    end0   = now.strftime("%Y-%m-%d")

    out["secagg_bearer"] = await _raw(FM_SECAGG,
        {"data_id": "2330", "start_date": start5, "end_date": end0, "token": FINMIND_TOKEN}, True)
    out["secagg_token_only"] = await _raw(FM_SECAGG,
        {"data_id": "2330", "start_date": start5, "end_date": end0, "token": FINMIND_TOKEN}, False)
    out["daily_report_endpoint"] = await _raw(
        "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report",
        {"data_id": "2330", "date": _last_weekday_str(), "token": FINMIND_TOKEN}, False)
    out["gov_dataset_candidates"] = await _discover_datasets(["government", "bank"])
    for ds_name in (out["gov_dataset_candidates"] or ["TaiwanStockGovernmentBankBuySell"])[:3]:
        out[f"govbank_{ds_name[:24]}"] = await _raw(FM_DATA,
            {"dataset": ds_name,
             "start_date": (now - timedelta(days=16)).strftime("%Y-%m-%d"), "token": FINMIND_TOKEN})
    out["moneyflow"] = await _raw(FM_DATA,
        {"dataset": "TaiwanStockIndustryChainMoneyFlow",
         "start_date": _last_weekday_str(), "token": FINMIND_TOKEN})
    out["snapshot_2330"] = await _raw(FM_DATA,
        {"dataset": "taiwan_stock_tick_snapshot", "data_id": "2330", "token": FINMIND_TOKEN})
    out["snapshot_001"] = await _raw(FM_DATA,
        {"dataset": "taiwan_stock_tick_snapshot", "data_id": "001", "token": FINMIND_TOKEN})
    out["kbar_2330"] = await _raw(FM_DATA,
        {"dataset": "TaiwanStockKBar", "data_id": "2330",
         "start_date": _last_weekday_str(), "token": FINMIND_TOKEN})
    return out

def _last_weekday_str() -> str:
    d = datetime.now(TW_TZ)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

@router.get("/verify/{ticker}")
async def branch_verify(ticker: str, date: str = ""):
    """對帳端點：不做任何換算，直接檢驗 Σ分點買 = Σ分點賣 = 官方成交量
    buy_over_volume ≈ 1.0  → 資料完整且單位為股
    buy_over_volume ≈ 0.001 → 單位其實是張（顯示層要修）
    buy_over_volume 明顯 < 0.9 → 該日資料尚未爬完整
    """
    try:
        if not date:
            date = _last_weekday_str()
        url = "https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report"
        async with httpx.AsyncClient(timeout=40) as cli:
            r = await cli.get(url, params={"data_id": ticker, "date": date,
                                           "token": FINMIND_TOKEN})
        try:
            rows = _parse(r.json(), f"分點 {date}")
        except Exception as pe:
            return {"error": str(pe), "date": date}

        sum_buy  = sum(float(x.get("buy", 0) or 0) for x in rows)
        sum_sell = sum(float(x.get("sell", 0) or 0) for x in rows)
        branches = {str(x.get("securities_trader_id", "")) or x.get("securities_trader", "")
                    for x in rows}

        vol, close = None, None
        try:
            vrows = await _fm("TaiwanStockPrice", date, date, ticker)
            if vrows:
                vol   = float(vrows[-1].get("Trading_Volume", 0) or 0)
                close = float(vrows[-1].get("close", 0) or 0)
        except Exception:
            pass

        return {
            "ticker": ticker, "date": date,
            "row_count": len(rows), "branch_count": len(branches),
            "sum_buy_raw": int(sum_buy), "sum_sell_raw": int(sum_sell),
            "official_volume_shares": int(vol) if vol else None,
            "official_close": close,
            "buy_over_volume":  round(sum_buy / vol, 4) if vol else None,
            "sell_over_volume": round(sum_sell / vol, 4) if vol else None,
            "buy_sell_diff_pct": round((sum_buy - sum_sell) / sum_buy * 100, 2) if sum_buy else None,
            "sample_rows": rows[:5],
        }
    except Exception as e:
        return {"error": str(e)}

# ── 融資戶平均成本推估 ──────────────────────────────────────────

MARGIN_LOAN_RATIO = 0.60      # 上市股票融資成數（上櫃常為 0.5，實際依標的）
CALL_LINE = 1.30              # 追繳線 130%


@router.get("/margin-cost/{ticker}")
async def margin_cost(ticker: str, days: int = 180, loan_ratio: float = MARGIN_LOAN_RATIO):
    """融資戶平均成本推估（移動加權平均法）

    方法：每日融資買進張數以當日 VWAP 計入成本，賣出／現償以當時平均成本扣除。
    這是「推估」不是公布數據，各家軟體因方法不同會有差異。
    """
    try:
        start = (datetime.now(TW_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        end   = datetime.now(TW_TZ).strftime("%Y-%m-%d")

        mrows = await _fm("TaiwanStockMarginPurchaseShortSale", start, end, ticker)
        prows = await _fm("TaiwanStockPrice", start, end, ticker)
        if not mrows or not prows:
            return {"error": "融資或股價資料不足"}

        px = {}
        for r in prows:
            vol = float(r.get("Trading_Volume", 0) or 0)
            money = float(r.get("Trading_money", 0) or 0)
            close = float(r.get("close", 0) or 0)
            hi, lo = float(r.get("max", 0) or 0), float(r.get("min", 0) or 0)
            vwap = money / vol if vol > 0 and money > 0 else \
                   ((hi + lo + close) / 3 if close > 0 else close)
            if vwap > 0:
                px[r["date"]] = {"vwap": round(vwap, 2), "close": close}

        mrows.sort(key=lambda x: x["date"])
        mrows = [r for r in mrows if r["date"] in px]
        if len(mrows) < 20:
            return {"error": f"可用交易日僅 {len(mrows)} 天，樣本不足"}

        def g(r, k):
            try:
                return float(r.get(k, 0) or 0)
            except Exception:
                return 0.0

        # 恆等式驗證（順便確認單位一致）
        idbad = 0
        for r in mrows:
            lhs = g(r, "MarginPurchaseTodayBalance")
            rhs = (g(r, "MarginPurchaseYesterdayBalance") + g(r, "MarginPurchaseBuy")
                   - g(r, "MarginPurchaseSell") - g(r, "MarginPurchaseCashRepayment"))
            if abs(lhs - rhs) > 1:
                idbad += 1

        first = mrows[0]
        balance = g(first, "MarginPurchaseYesterdayBalance") or g(first, "MarginPurchaseTodayBalance")
        cost = px[first["date"]]["vwap"]          # 初始成本假設＝區間首日均價
        init_balance = balance
        cum_buy = 0.0
        history = []

        for r in mrows:
            d = r["date"]
            v = px[d]["vwap"]
            buy  = g(r, "MarginPurchaseBuy")
            out  = g(r, "MarginPurchaseSell") + g(r, "MarginPurchaseCashRepayment")
            if buy > 0:
                total = balance + buy
                cost = (balance * cost + buy * v) / total if total > 0 else v
                balance = total
                cum_buy += buy
            if out > 0:
                balance = max(0.0, balance - out)     # 賣出以平均成本出場，成本不變
            history.append({"date": d, "balance_lots": round(balance),
                            "avg_cost": round(cost, 2), "vwap": v})

        latest = mrows[-1]
        cur_balance = g(latest, "MarginPurchaseTodayBalance")
        avg_cost = round(cost, 2)

        # 現價
        cur_price = None
        try:
            from services.yfinance_service import get_tw_realtime_price
            rt = await get_tw_realtime_price(ticker)
            if rt and rt.get("current_price"):
                cur_price = float(rt["current_price"])
        except Exception:
            pass
        if not cur_price:
            cur_price = px[latest["date"]]["close"]

        pnl_pct = round((cur_price - avg_cost) / avg_cost * 100, 2) if avg_cost else None
        call_price = round(avg_cost * loan_ratio * CALL_LINE, 2)
        to_call_pct = round((call_price - cur_price) / cur_price * 100, 2) if cur_price else None
        maint = round(cur_price / (avg_cost * loan_ratio) * 100, 1) if avg_cost else None

        # 區間內買進量佔目前餘額比例 → 初始假設被沖淡的程度
        coverage = round(cum_buy / cur_balance, 2) if cur_balance > 0 else None
        if coverage is None:
            reliability = "無法評估"
        elif coverage >= 3:
            reliability = "高（區間內換手充分，初始假設影響已極小）"
        elif coverage >= 1.5:
            reliability = "中（初始假設影響有限）"
        else:
            reliability = "低（區間內換手不足，結果受初始假設影響大，建議拉長 days）"

        if pnl_pct is None:
            meaning = "資料不足"
        elif pnl_pct <= -15:
            meaning = (f"融資戶平均套牢 {abs(pnl_pct)}%（成本約 ${avg_cost}）→ "
                       f"反彈到成本區附近會遇到大量解套賣壓，那裡是壓力不是目標；"
                       f"且平均維持率僅 {maint}%，再跌容易出現斷頭賣壓加速下殺。")
        elif pnl_pct < -3:
            meaning = (f"融資戶小幅套牢 {abs(pnl_pct)}%（成本約 ${avg_cost}）→ "
                       f"${avg_cost} 是短線壓力區，站上並站穩才算解套換手完成。")
        elif pnl_pct <= 5:
            meaning = (f"現價貼近融資成本 ${avg_cost} → 這是融資戶的多空分界，"
                       f"跌破容易觸發停損與追繳的連鎖賣壓，是關鍵防守位。")
        elif pnl_pct <= 20:
            meaning = (f"融資戶平均獲利 {pnl_pct}%（成本約 ${avg_cost}）→ "
                       f"有獲利墊底，短線賣壓較輕；${avg_cost} 可視為回檔的第一支撐參考。")
        else:
            meaning = (f"融資戶平均獲利 {pnl_pct}%（成本約 ${avg_cost}）→ "
                       f"獲利豐厚代表隨時可能了結，若出現爆量長黑要留意集體出場；"
                       f"追繳價 ${call_price} 距現價很遠，短期無斷頭風險。")

        return {
            "ticker": ticker, "name": await _name_of_async(ticker),
            "data_date": latest["date"],
            "current_price": cur_price,
            "avg_cost_est": avg_cost,
            "margin_holder_pnl_pct": pnl_pct,
            "balance_lots": round(cur_balance),
            "balance_change_5d_lots": round(cur_balance - g(mrows[-6], "MarginPurchaseTodayBalance"))
                                      if len(mrows) >= 6 else None,
            "assumed_loan_ratio": loan_ratio,
            "call_price_est": call_price,
            "distance_to_call_pct": to_call_pct,
            "avg_maintenance_pct": maint,
            "what_this_means": meaning,
            "reliability": reliability,
            "in_window_buy_over_balance": coverage,
            "window_days": len(mrows),
            "window": f'{mrows[0]["date"]} ~ {latest["date"]}',
            "identity_check": {"rows": len(mrows), "mismatch": idbad,
                               "formula": "今日餘額 = 昨日餘額 + 融資買 - 融資賣 - 現金償還",
                               "verdict": "PASS" if idbad == 0 else f"有 {idbad} 日不符"},
            "history": history[-40:],
            "limitations": [
                "融資戶成本無官方公布，此為推估值；不同軟體因演算法不同會有差異",
                f"初始成本以區間首日均價假設（區間內買進量為目前餘額的 {coverage} 倍）",
                "融資成數以 " + str(int(loan_ratio * 100)) + "% 計，實際依標的與券商而異；"
                "追繳價為對應之估算值",
                "現金償還不經市場賣出，會使餘額下降但無實際賣壓",
            ],
            "checked_at": datetime.now(TW_TZ).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


async def _name_of_async(t: str) -> str:
    try:
        from services.finmind import get_tw_name_map
        m = await get_tw_name_map()
        if m.get(str(t)):
            return m[str(t)]
    except Exception:
        pass
    return _name_of(t)


def _name_of(t: str) -> str:
    try:
        from routers.scan import TW_STOCK_LIST
        return TW_STOCK_LIST.get(t, t)
    except Exception:
        return t


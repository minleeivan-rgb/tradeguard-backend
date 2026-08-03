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

        return {"ticker": ticker, "date": latest, "window_dates": dates,
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
        return {"ticker": ticker, "date": use_date, "bars": bars,
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

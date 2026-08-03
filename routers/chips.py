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

async def _fm(dataset: str, start: str, end: str = None, data_id: str = None) -> list:
    params = {"dataset": dataset, "start_date": start, "token": FINMIND_TOKEN}
    if end:
        params["end_date"] = end
    if data_id:
        params["data_id"] = data_id
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.get(FM_DATA, params=params,
                        headers={"Authorization": f"Bearer {FINMIND_TOKEN}"})
    j = r.json()
    if j.get("status") != 200:
        raise Exception(f"{dataset}: {j.get('msg', 'unknown')}")
    return j.get("data", [])

@router.get("/branch/{ticker}")
async def branch_chips(ticker: str, days: int = 5):
    """分點籌碼：近N日各券商分點買賣統計"""
    try:
        start = (datetime.now(TW_TZ) - timedelta(days=days + 9)).strftime("%Y-%m-%d")
        end   = datetime.now(TW_TZ).strftime("%Y-%m-%d")
        params = {"data_id": ticker, "start_date": start, "end_date": end,
                  "token": FINMIND_TOKEN}
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(FM_SECAGG, params=params,
                            headers={"Authorization": f"Bearer {FINMIND_TOKEN}"})
        j = r.json()
        if j.get("status") != 200:
            return {"error": f"分點資料: {j.get('msg', 'unknown')}"}
        rows = j.get("data", [])
        if not rows:
            return {"error": "近期無分點資料"}

        dates = sorted({r["date"] for r in rows})[-days:]
        rows = [r for r in rows if r["date"] in dates]
        latest = dates[-1]

        # 彙整：branch → {daily net, 累計}
        agg = {}
        for r in rows:
            key = r.get("securities_trader_id", "")
            name = r.get("securities_trader", key)
            b = float(r.get("buy_volume", 0) or 0)
            s = float(r.get("sell_volume", 0) or 0)
            a = agg.setdefault(key, {"name": name, "daily": {}, "cum": 0.0,
                                     "buy_price": None})
            net = b - s
            a["daily"][r["date"]] = a["daily"].get(r["date"], 0) + net
            a["cum"] += net
            if r["date"] == latest and b > 0 and r.get("buy_price"):
                a["buy_price"] = float(r["buy_price"])

        today_list, streak_list = [], []
        total_buy_today = 0.0
        for key, a in agg.items():
            tn = a["daily"].get(latest, 0)
            if tn > 0:
                total_buy_today += tn
            pos_days = sum(1 for d in dates if a["daily"].get(d, 0) > 0)
            item = {"branch": a["name"], "foreign": _is_foreign(a["name"]),
                    "today_lots": round(tn / 1000, 0),
                    "cum_lots": round(a["cum"] / 1000, 0),
                    "pos_days": pos_days, "days": len(dates),
                    "buy_price": a["buy_price"]}
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
                "note": "連買=近5日中≥4日淨買超；外資系依券商名稱判斷"}
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

@router.get("/govbank")
async def govbank():
    """八大行庫買賣（國安基金動向代理）"""
    try:
        start = (datetime.now(TW_TZ) - timedelta(days=16)).strftime("%Y-%m-%d")
        rows = await _fm("TaiwanstockGovernmentBankBuySell", start)
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

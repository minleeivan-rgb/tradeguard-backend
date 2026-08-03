"""
主動式ETF追蹤
- /etf/list      主動式ETF清單（Free）
- /etf/overview  全體主動ETF今日集體淨買/淨賣 Top（Sponsor）
- /etf/{id}/detail  單一ETF：今日買賣異動 + 持股明細與權重變化（Sponsor）
資料集：TaiwanStockActiveETFInfo / TaiwanStockActiveETFHoldingChange / TaiwanStockActiveETFHolding
"""
import os
from fastapi import APIRouter
from datetime import datetime, timedelta, timezone
import httpx

router = APIRouter(prefix="/etf", tags=["etf"])

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))

SPONSOR_MSG = "此資料集需 FinMind Sponsor 方案（$999/月）。你目前是 Backer，升級後此頁立即可用：finmindtrade.com"

async def _fm(dataset: str, start: str, end: str = None, data_id: str = None) -> list:
    params = {"dataset": dataset, "start_date": start, "token": FINMIND_TOKEN}
    if end:
        params["end_date"] = end
    if data_id:
        params["data_id"] = data_id
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.get(FINMIND_BASE, params=params)
    j = r.json()
    if j.get("status") != 200:
        msg = str(j.get("msg", ""))
        if "402" in str(j.get("status")) or "sponsor" in msg.lower() or "權限" in msg or "permission" in msg.lower():
            raise PermissionError(SPONSOR_MSG)
        raise Exception(f"{dataset}: {msg}")
    return j.get("data", [])

async def _latest_rows(dataset: str, data_id: str = None, lookback: int = 8):
    """從今天往回找最近一個有資料的日期，回傳 (date, rows)"""
    d = datetime.now(TW_TZ)
    for _ in range(lookback):
        if d.weekday() < 5:
            ds = d.strftime("%Y-%m-%d")
            rows = await _fm(dataset, ds, ds, data_id)
            if rows:
                return ds, rows
        d -= timedelta(days=1)
    return None, []

@router.get("/list")
async def etf_list():
    try:
        rows = await _fm("TaiwanStockActiveETFInfo", "2025-01-01")
        seen = {}
        for r in rows:
            seen[r["stock_id"]] = {"stock_id": r["stock_id"],
                                   "name": r.get("stock_name", r["stock_id"]),
                                   "category": r.get("category", ""),
                                   "type": r.get("type", "")}
        items = sorted(seen.values(), key=lambda x: x["stock_id"])
        return {"count": len(items), "items": items}
    except Exception as e:
        return {"error": str(e), "items": []}

@router.get("/overview")
async def etf_overview():
    """全體主動式ETF最新一日：集體淨買超 / 淨賣超個股 Top"""
    try:
        date, chg = await _latest_rows("TaiwanStockActiveETFHoldingChange")
        if not date:
            return {"error": "近期無異動資料（資料每日晚間更新）"}
        # 同日持股明細 → 估價（market_value/shares）與權重
        _, hold = await _latest_rows("TaiwanStockActiveETFHolding")
        price_map, weight_map = {}, {}
        for h in hold:
            cid = str(h.get("component_stock_id", ""))
            sh  = float(h.get("shares", 0) or 0)
            mv  = float(h.get("market_value", 0) or 0)
            if cid and sh > 0 and mv > 0 and str(h.get("currency", "TWD")).upper() in ("TWD", "NTD", ""):
                price_map.setdefault(cid, mv / sh)
            w = float(h.get("weight", 0) or 0)
            weight_map[(str(h.get("stock_id")), cid)] = w

        agg = {}
        for r in chg:
            cid  = str(r.get("component_stock_id", ""))
            if not (cid.isdigit() and 4 <= len(cid) <= 5):
                continue
            name = r.get("component_stock_name", cid)
            buy  = float(r.get("buy", 0) or 0)
            sell = float(r.get("sell", 0) or 0)
            net  = buy - sell
            a = agg.setdefault(cid, {"ticker": cid, "name": name, "net_shares": 0.0,
                                     "buy_etfs": set(), "sell_etfs": set()})
            a["net_shares"] += net
            etf = str(r.get("stock_id", ""))
            if net > 0:
                a["buy_etfs"].add(etf)
            elif net < 0:
                a["sell_etfs"].add(etf)

        rows = []
        for cid, a in agg.items():
            price = price_map.get(cid)
            est_val = round(a["net_shares"] * price / 1e8, 2) if price else None
            rows.append({"ticker": cid, "name": a["name"],
                         "net_lots": round(a["net_shares"] / 1000, 0),
                         "est_value_yi": est_val,
                         "buy_etf_count": len(a["buy_etfs"]),
                         "sell_etf_count": len(a["sell_etfs"])})
        keyf = lambda x: (x["est_value_yi"] if x["est_value_yi"] is not None else x["net_lots"] / 1000)
        buys  = sorted([x for x in rows if x["net_lots"] > 0], key=keyf, reverse=True)[:15]
        sells = sorted([x for x in rows if x["net_lots"] < 0], key=keyf)[:15]
        return {"date": date, "etf_count": len({str(r.get('stock_id')) for r in chg}),
                "top_buys": buys, "top_sells": sells,
                "note": "含申購贖回造成的等比例增減；權重變化請看單一ETF明細"}
    except PermissionError as e:
        return {"error": str(e), "tier_required": "Sponsor"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/{etf_id}/detail")
async def etf_detail(etf_id: str):
    """單一主動ETF：今日買賣異動 + 持股明細（權重、權重日變化）"""
    try:
        start = (datetime.now(TW_TZ) - timedelta(days=12)).strftime("%Y-%m-%d")
        end   = datetime.now(TW_TZ).strftime("%Y-%m-%d")
        hold  = await _fm("TaiwanStockActiveETFHolding", start, end, etf_id)
        if not hold:
            return {"error": "此ETF近期無持股資料"}
        dates = sorted({h["date"] for h in hold})
        d_now = dates[-1]
        d_prev = dates[-2] if len(dates) > 1 else None
        cur  = [h for h in hold if h["date"] == d_now]
        prevw = {str(h.get("component_stock_id")): float(h.get("weight", 0) or 0)
                 for h in hold if d_prev and h["date"] == d_prev}

        holdings = []
        for h in cur:
            cid = str(h.get("component_stock_id", ""))
            w   = float(h.get("weight", 0) or 0)
            pw  = prevw.get(cid)
            holdings.append({
                "ticker": cid, "name": h.get("component_stock_name", cid),
                "asset_type": h.get("asset_type", ""),
                "weight": round(w, 2),
                "weight_delta": round(w - pw, 2) if pw is not None else None,
                "shares_lots": round(float(h.get("shares", 0) or 0) / 1000, 0),
                "market_value_yi": round(float(h.get("market_value", 0) or 0) / 1e8, 2),
            })
        holdings.sort(key=lambda x: -x["weight"])

        changes = []
        try:
            chg = await _fm("TaiwanStockActiveETFHoldingChange", d_now, d_now, etf_id)
            for r in chg:
                buy  = float(r.get("buy", 0) or 0)
                sell = float(r.get("sell", 0) or 0)
                changes.append({"ticker": str(r.get("component_stock_id", "")),
                                "name": r.get("component_stock_name", ""),
                                "buy_lots": round(buy / 1000, 0),
                                "sell_lots": round(sell / 1000, 0),
                                "net_lots": round((buy - sell) / 1000, 0)})
            changes.sort(key=lambda x: -abs(x["net_lots"]))
        except Exception:
            pass

        active_adds = [h for h in holdings if h["weight_delta"] is not None and h["weight_delta"] >= 0.15]
        active_cuts = [h for h in holdings if h["weight_delta"] is not None and h["weight_delta"] <= -0.15]
        return {"etf_id": etf_id, "date": d_now, "prev_date": d_prev,
                "holdings": holdings[:30], "changes": changes[:20],
                "active_adds": active_adds[:10], "active_cuts": active_cuts[:10],
                "note": "權重變化 ≥±0.15% 視為主動調整（排除申贖等比例效果）"}
    except PermissionError as e:
        return {"error": str(e), "tier_required": "Sponsor"}
    except Exception as e:
        return {"error": str(e)}

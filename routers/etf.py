"""
主動式ETF追蹤（區間查詢版）
- /etf/list        主動式ETF清單
- /etf/overview    全體集體買賣超（自動取來源最新有資料日）
- /etf/{id}/detail 單一ETF持股與權重變化
- /etf/freshness   逐檔新鮮度：每檔ETF的最新資料日，一眼看誰落後
- /etf/debug       來源真相：近14日每天有幾筆資料、原始回應
"""
import os
from fastapi import APIRouter
from datetime import datetime, timedelta, timezone
import httpx

router = APIRouter(prefix="/etf", tags=["etf"])

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FINMIND_BASE  = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))

SPONSOR_MSG = "此資料集需 FinMind Sponsor 方案。若已升級仍出現此訊息，請確認 Railway 的 FINMIND_TOKEN 是新方案的 token"

def _tw_now():
    return datetime.now(TW_TZ)

def _last_trading_day(offset: int = 0) -> str:
    d = _tw_now() - timedelta(days=offset)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def _trading_days_between(d_from: str, d_to: str) -> int:
    try:
        a = datetime.strptime(d_from, "%Y-%m-%d").date()
        b = datetime.strptime(d_to, "%Y-%m-%d").date()
    except Exception:
        return 0
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n

async def _fm(dataset: str, start: str, end: str = None, data_id: str = None) -> list:
    params = {"dataset": dataset, "start_date": start, "token": FINMIND_TOKEN}
    if end:
        params["end_date"] = end
    if data_id:
        params["data_id"] = data_id
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(FINMIND_BASE, params=params)
    try:
        j = r.json()
    except Exception:
        raise Exception(f"{dataset} HTTP {r.status_code}: {r.text[:180]}")
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        return j["data"]
    msg = str(j)[:200]
    if "402" in msg or "sponsor" in msg.lower() or "permission" in msg.lower():
        raise PermissionError(SPONSOR_MSG)
    raise Exception(f"{dataset}: {msg}")

async def _bulk_day(dataset: str, date: str) -> list:
    """全市場批次查詢＝單日模式（FinMind 不帶 data_id 時會忽略 end_date）"""
    return await _fm(dataset, date, date)

def _recent_trading_days(n: int = 10) -> list:
    """由今天往回列出 n 個交易日（新→舊）"""
    out, d = [], _tw_now()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return out

async def _probe_days(dataset: str, n: int = 10) -> dict:
    """逐日探測，回傳每個交易日的筆數（0 = 來源當天無資料）"""
    counts = {}
    for ds in _recent_trading_days(n):
        try:
            counts[ds] = len(await _bulk_day(dataset, ds))
        except PermissionError:
            raise
        except Exception:
            counts[ds] = None      # None = 查詢失敗（非「無資料」）
    return counts

async def _latest_bulk(dataset: str, max_days: int = 10):
    """往回找最近一個有資料的交易日，回傳 (date, rows, 探測記錄)"""
    probe = {}
    for ds in _recent_trading_days(max_days):
        try:
            rows = await _bulk_day(dataset, ds)
        except PermissionError:
            raise
        except Exception:
            probe[ds] = None
            continue
        probe[ds] = len(rows)
        if rows:
            return ds, rows, probe
    return None, [], probe

async def _latest_complete_bulk(dataset: str, max_days: int = 10, min_ratio: float = 0.6):
    """找最近一個「資料完整」的交易日。
    完整度 = 該日筆數 / 前面有資料日的最大筆數；低於 min_ratio 視為爬取不完整並往前退。
    回傳 (date, rows, probe, quality)"""
    found = []          # [(date, rows)]
    probe = {}
    for ds in _recent_trading_days(max_days):
        try:
            rows = await _bulk_day(dataset, ds)
        except PermissionError:
            raise
        except Exception:
            probe[ds] = None
            continue
        probe[ds] = len(rows)
        if rows:
            found.append((ds, rows))
            if len(found) >= 4:
                break
    if not found:
        return None, [], probe, {"status": "no_data"}

    baseline = max(len(r) for _, r in found)
    quality = {"baseline_rows": baseline, "skipped": []}
    for ds, rows in found:                       # found 已由新到舊
        ratio = len(rows) / baseline if baseline else 0
        if ratio >= min_ratio:
            quality.update({"status": "ok", "used_date": ds,
                            "rows": len(rows), "completeness_pct": round(ratio * 100, 1)})
            return ds, rows, probe, quality
        quality["skipped"].append({"date": ds, "rows": len(rows),
                                   "completeness_pct": round(ratio * 100, 1)})
    ds, rows = found[0]
    quality.update({"status": "all_incomplete", "used_date": ds, "rows": len(rows),
                    "completeness_pct": round(len(rows) / baseline * 100, 1) if baseline else 0})
    return ds, rows, probe, quality

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

@router.get("/freshness")
async def etf_freshness(days: int = 8):
    """逐檔新鮮度：對最近N個交易日逐日批次查詢，group by ETF 取最新日"""
    try:
        latest_by_etf, rows_per_date = {}, {}
        for ds in _recent_trading_days(days):
            try:
                rows = await _bulk_day("TaiwanStockActiveETFHolding", ds)
            except PermissionError:
                raise
            except Exception:
                rows_per_date[ds] = None
                continue
            rows_per_date[ds] = len(rows)
            for r in rows:
                sid = str(r.get("stock_id", ""))
                if sid:
                    if sid not in latest_by_etf or ds > latest_by_etf[sid]:
                        latest_by_etf[sid] = ds
        if not latest_by_etf:
            return {"error": f"最近{days}個交易日皆無主動ETF持股資料",
                    "rows_per_date": dict(sorted(rows_per_date.items()))}
        newest = max(latest_by_etf.values())
        expected = _last_trading_day(0)
        items = sorted([{"etf": k, "latest_date": v,
                         "lag_vs_newest": _trading_days_between(v, newest)}
                        for k, v in latest_by_etf.items()],
                       key=lambda x: (x["latest_date"], x["etf"]))
        return {"source_newest_date": newest,
                "expected_latest_trading_day": expected,
                "source_lag_trading_days": _trading_days_between(newest, expected),
                "etf_count": len(items),
                "stale_etfs": [x for x in items if x["lag_vs_newest"] > 0],
                "all": items,
                "rows_per_date": dict(sorted(rows_per_date.items())),
                "note": "rows_per_date 為 0 代表來源當日確實無資料；null 代表查詢失敗"}
    except PermissionError as e:
        return {"error": str(e), "tier_required": "Sponsor"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/debug")
async def etf_debug(days: int = 10):
    """來源真相：逐日批次探測，0 = 來源當日無資料"""
    out = {"tw_now": _tw_now().isoformat(),
           "expected_last_trading_day": _last_trading_day(0),
           "probe_note": "全市場批次查詢為單日模式，故逐日探測；0=無資料，null=查詢失敗"}
    for key, ds_name in [("holding", "TaiwanStockActiveETFHolding"),
                         ("holding_change", "TaiwanStockActiveETFHoldingChange")]:
        try:
            probe = await _probe_days(ds_name, days)
            have = [d for d, n in probe.items() if n]
            out[key] = {"ok": True,
                        "latest_date_with_data": max(have) if have else None,
                        "rows_per_trading_day": probe}
        except Exception as e:
            out[key] = {"ok": False, "error": str(e)}
    for etf in ("00981A", "00980A", "00982A"):
        try:
            start = (_tw_now() - timedelta(days=16)).strftime("%Y-%m-%d")
            end   = _tw_now().strftime("%Y-%m-%d")
            rows  = await _fm("TaiwanStockActiveETFHolding", start, end, etf)
            byd = {}
            for r in rows:
                byd[r["date"]] = byd.get(r["date"], 0) + 1
            out[f"holding_{etf}"] = {"ok": True,
                                     "latest_date": max(byd) if byd else None,
                                     "dates_and_counts": dict(sorted(byd.items()))}
        except Exception as e:
            out[f"holding_{etf}"] = {"ok": False, "error": str(e)}
    return out

@router.get("/overview")
async def etf_overview():
    try:
        date, chg, probe, quality = await _latest_complete_bulk(
            "TaiwanStockActiveETFHoldingChange", 10)
        if not date:
            return {"error": "近10個交易日無異動資料", "rows_per_trading_day": probe}
        h_date, hold, _, h_quality = await _latest_complete_bulk(
            "TaiwanStockActiveETFHolding", 10)
        price_map = {}
        for h in (hold or []):
            cid = str(h.get("component_stock_id", ""))
            sh  = float(h.get("shares", 0) or 0)
            mv  = float(h.get("market_value", 0) or 0)
            if cid and sh > 0 and mv > 0 and str(h.get("currency", "TWD")).upper() in ("TWD", "NTD", ""):
                price_map.setdefault(cid, mv / sh)

        agg = {}
        for r in chg:
            cid = str(r.get("component_stock_id", ""))
            if not (cid.isdigit() and 4 <= len(cid) <= 5):
                continue
            buy  = float(r.get("buy", 0) or 0)
            sell = float(r.get("sell", 0) or 0)
            net  = buy - sell
            a = agg.setdefault(cid, {"name": r.get("component_stock_name", cid),
                                     "net": 0.0, "b": set(), "s": set()})
            a["net"] += net
            etf = str(r.get("stock_id", ""))
            (a["b"] if net > 0 else a["s"]).add(etf)

        rows = []
        for cid, a in agg.items():
            price = price_map.get(cid)
            rows.append({"ticker": cid, "name": a["name"],
                         "net_lots": round(a["net"] / 1000, 0),
                         "est_value_yi": round(a["net"] * price / 1e8, 2) if price else None,
                         "buy_etf_count": len(a["b"]), "sell_etf_count": len(a["s"])})
        keyf = lambda x: (x["est_value_yi"] if x["est_value_yi"] is not None else x["net_lots"] / 1000)
        expected = _last_trading_day(0)
        lag = _trading_days_between(date, expected)
        return {"date": date, "holding_date": h_date,
                "expected_latest_trading_day": expected,
                "source_lag_trading_days": lag,
                "etf_count": len({str(r.get("stock_id")) for r in chg}),
                "rows_per_trading_day": probe,
                "data_quality": quality, "holding_quality": h_quality,
                "top_buys": sorted([x for x in rows if x["net_lots"] > 0], key=keyf, reverse=True)[:15],
                "top_sells": sorted([x for x in rows if x["net_lots"] < 0], key=keyf)[:15],
                "note": "含申購贖回造成的等比例增減；權重變化請看單一ETF明細"}
    except PermissionError as e:
        return {"error": str(e), "tier_required": "Sponsor"}
    except Exception as e:
        return {"error": str(e)}

@router.get("/{etf_id}/detail")
async def etf_detail(etf_id: str):
    try:
        start = (_tw_now() - timedelta(days=14)).strftime("%Y-%m-%d")
        end   = _tw_now().strftime("%Y-%m-%d")
        hold  = await _fm("TaiwanStockActiveETFHolding", start, end, etf_id)
        if not hold:
            return {"error": "此ETF近14日無持股資料"}
        dates = sorted({h["date"] for h in hold})
        d_now = dates[-1]
        d_prev = dates[-2] if len(dates) > 1 else None
        cur = [h for h in hold if h["date"] == d_now]
        prevw = {str(h.get("component_stock_id")): float(h.get("weight", 0) or 0)
                 for h in hold if d_prev and h["date"] == d_prev}

        holdings = []
        for h in cur:
            cid = str(h.get("component_stock_id", ""))
            w = float(h.get("weight", 0) or 0)
            pw = prevw.get(cid)
            holdings.append({"ticker": cid, "name": h.get("component_stock_name", cid),
                             "asset_type": h.get("asset_type", ""), "weight": round(w, 2),
                             "weight_delta": round(w - pw, 2) if pw is not None else None,
                             "shares_lots": round(float(h.get("shares", 0) or 0) / 1000, 0),
                             "market_value_yi": round(float(h.get("market_value", 0) or 0) / 1e8, 2)})
        holdings.sort(key=lambda x: -x["weight"])

        changes = []
        try:
            for r in await _fm("TaiwanStockActiveETFHoldingChange", d_now, d_now, etf_id):
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

        expected = _last_trading_day(0)
        return {"etf_id": etf_id, "date": d_now, "prev_date": d_prev,
                "available_dates": dates,
                "expected_latest_trading_day": expected,
                "lag_trading_days": _trading_days_between(d_now, expected),
                "holdings": holdings[:30], "changes": changes[:20],
                "active_adds": [h for h in holdings if h["weight_delta"] is not None and h["weight_delta"] >= 0.15][:10],
                "active_cuts": [h for h in holdings if h["weight_delta"] is not None and h["weight_delta"] <= -0.15][:10],
                "note": "權重變化 ≥±0.15% 視為主動調整（排除申贖等比例效果）"}
    except PermissionError as e:
        return {"error": str(e), "tier_required": "Sponsor"}
    except Exception as e:
        return {"error": str(e)}

# ── 官方來源探測（繞過 FinMind 延遲）────────────────────────────

TWSE_CANDIDATES = [
    ("openapi_pcf",      "https://openapi.twse.com.tw/v1/ETFReport/ETFPCF", {}),
    ("openapi_etfrpt",   "https://openapi.twse.com.tw/v1/ETFReport/ETFReport", {}),
    ("rwd_pcf_date",     "https://www.twse.com.tw/rwd/zh/ETF/etfPCF",
                          {"response": "json", "date": "{ymd}"}),
    ("rwd_pcf_nodate",   "https://www.twse.com.tw/rwd/zh/ETF/etfPCF", {"response": "json"}),
    ("legacy_pcf",       "https://www.twse.com.tw/exchangeReport/ETFPCF",
                          {"response": "json", "date": "{ymd}"}),
    ("rwd_etf_daily",    "https://www.twse.com.tw/rwd/zh/ETF/etfDaily",
                          {"response": "json", "date": "{ymd}"}),
]

@router.get("/probe-official")
async def probe_official(date: str = ""):
    """探測證交所官方 ETF 持股/PCF 端點，回報哪個可用（含真實回應片段）"""
    d = (date or _last_trading_day(0)).replace("-", "")
    out = {"probe_date_ymd": d, "results": {}}
    headers = {"User-Agent": "Mozilla/5.0", "accept": "application/json",
               "Referer": "https://www.twse.com.tw/"}
    for name, url, params in TWSE_CANDIDATES:
        pr = {k: v.replace("{ymd}", d) for k, v in params.items()}
        try:
            async with httpx.AsyncClient(timeout=25, verify=False) as cli:
                r = await cli.get(url, params=pr, headers=headers)
            info = {"http": r.status_code, "content_type": r.headers.get("content-type", "")[:60]}
            try:
                j = r.json()
                if isinstance(j, list):
                    info["shape"] = "list"
                    info["rows"] = len(j)
                    if j:
                        info["sample_keys"] = list(j[0].keys())[:14]
                        info["sample_row"] = {k: str(v)[:40] for k, v in list(j[0].items())[:8]}
                elif isinstance(j, dict):
                    info["shape"] = "dict"
                    info["keys"] = list(j.keys())[:12]
                    info["stat"] = j.get("stat")
                    data = j.get("data") or j.get("aaData")
                    if isinstance(data, list):
                        info["rows"] = len(data)
                        info["fields"] = (j.get("fields") or j.get("columns") or [])[:14]
                        if data:
                            info["sample_row"] = [str(x)[:30] for x in data[0][:8]] \
                                if isinstance(data[0], list) else str(data[0])[:200]
            except Exception:
                info["not_json"] = r.text[:200]
            out["results"][name] = info
        except Exception as e:
            out["results"][name] = {"error": str(e)[:180]}
    return out

@router.get("/coverage")
async def etf_coverage(date: str = "", etf: str = ""):
    """指定日期的 ETF 覆蓋狀況：那天到底有哪幾檔 ETF 的資料（即時查 FinMind，不經快取）"""
    d = date or _last_trading_day(0)
    out = {"date": d, "queried_at": _tw_now().isoformat()}
    try:
        rows = await _bulk_day("TaiwanStockActiveETFHolding", d)
        by_etf = {}
        for r in rows:
            sid = str(r.get("stock_id", ""))
            by_etf[sid] = by_etf.get(sid, 0) + 1
        out["total_rows"] = len(rows)
        out["etf_count"] = len(by_etf)
        out["etfs_present"] = dict(sorted(by_etf.items()))
        try:
            info = await _fm("TaiwanStockActiveETFInfo", "2025-01-01")
            all_ids = sorted({str(x["stock_id"]) for x in info})
            out["etfs_missing"] = [x for x in all_ids if x not in by_etf]
        except Exception:
            pass
    except Exception as e:
        out["bulk_error"] = str(e)

    target = etf or "00981A"
    try:
        start = (_tw_now() - timedelta(days=16)).strftime("%Y-%m-%d")
        end   = _tw_now().strftime("%Y-%m-%d")
        rows2 = await _fm("TaiwanStockActiveETFHolding", start, end, target)
        byd = {}
        for r in rows2:
            byd[r["date"]] = byd.get(r["date"], 0) + 1
        out[f"{target}_dates"] = dict(sorted(byd.items()))
        out[f"{target}_latest"] = max(byd) if byd else None
        rows3 = await _fm("TaiwanStockActiveETFHolding", d, d, target)
        out[f"{target}_on_{d}"] = len(rows3)
    except Exception as e:
        out[f"{target}_error"] = str(e)
    return out


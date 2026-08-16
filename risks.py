"""
部位風險總覽
- /risk/overview/{user_id}  總曝險、族群集中度、整戶維持率、壓力測試

計算說明（重要，請對照券商對帳單）：
  整戶維持率 = 所有融資部位總市值 / 融資總借款
  這是台灣券商的整戶計算方式；本系統依你輸入的融資成數估算借款金額，
  實際借款以券商為準（券商另計利息、手續費，且成數可能因股而異）。
  追繳線 130%、斷頭線約 120%（各券商略異）。
"""
from fastapi import APIRouter
from datetime import datetime, timedelta, timezone
from database import db

router = APIRouter(prefix="/risk", tags=["risk"])
TW_TZ = timezone(timedelta(hours=8))

STRESS_LEVELS = [-3, -5, -10, -15, -20]


def _sector_of(ticker: str) -> str:
    try:
        from routers.scan import TW_THEME_SECTORS
        for s, lst in TW_THEME_SECTORS.items():
            if ticker in lst:
                return s
    except Exception:
        pass
    return "其他"


@router.get("/overview/{user_id}")
async def risk_overview(user_id: str):
    from services.yfinance_service import get_tw_realtime_price
    try:
        from routers.scan import resolve_tw_name
    except Exception:
        resolve_tw_name = None

    holdings = [h async for h in db.holdings.find({"user_id": user_id})]
    if not holdings:
        return {"error": "無持倉資料"}

    items = []
    for h in holdings:
        ticker = h["ticker"]
        market = h.get("market", "tw")
        shares = h.get("shares", 0) * (1000 if h.get("unit") == "lot" else 1)
        if shares <= 0:
            continue

        cur = h.get("current_price") or h.get("entry_price") or 0
        ptime = "DB 快照"
        if market == "tw":
            try:
                rt = await get_tw_realtime_price(ticker)
                if rt and rt.get("current_price"):
                    cur = rt["current_price"]
                    ptime = f'{rt.get("price_time", "")} {rt.get("source", "")}'.strip()
            except Exception:
                pass

        name = h.get("name", ticker)
        if market == "tw" and resolve_tw_name:
            try:
                name = await resolve_tw_name(ticker, h.get("name"), db, user_id)
            except Exception:
                pass

        entry = h.get("entry_price") or cur
        mv    = cur * shares
        cost  = entry * shares
        is_m  = bool(h.get("margin"))
        ratio = h.get("margin_ratio", 0.6) if is_m else 0.0
        loan  = cost * ratio

        items.append({
            "ticker": ticker, "name": name, "market": market,
            "sector": _sector_of(ticker) if market == "tw" else "美股",
            "shares": shares, "entry": entry, "current": cur, "price_time": ptime,
            "market_value": round(mv), "cost": round(cost),
            "pnl_pct": round((cur - entry) / entry * 100, 2) if entry else 0,
            "margin": is_m, "loan": round(loan), "equity": round(mv - loan),
        })

    if not items:
        return {"error": "持倉資料不完整（股數為 0）"}

    total_mv   = sum(x["market_value"] for x in items)
    total_loan = sum(x["loan"] for x in items)
    total_eq   = total_mv - total_loan
    leverage   = round(total_mv / total_eq, 2) if total_eq > 0 else None

    m_items = [x for x in items if x["margin"] and x["loan"] > 0]
    m_mv    = sum(x["market_value"] for x in m_items)
    m_loan  = sum(x["loan"] for x in m_items)
    maint   = round(m_mv / m_loan * 100, 1) if m_loan > 0 else None

    # ── 族群集中度 ──
    sec = {}
    for x in items:
        s = sec.setdefault(x["sector"], {"sector": x["sector"], "mv": 0, "tickers": []})
        s["mv"] += x["market_value"]
        s["tickers"].append(f'{x["ticker"]} {x["name"]}')
    sectors = []
    for s in sec.values():
        pct = round(s["mv"] / total_mv * 100, 1) if total_mv else 0
        sectors.append({
            "sector": s["sector"], "market_value": round(s["mv"]), "pct": pct,
            "tickers": s["tickers"],
            "level": "danger" if pct >= 40 else "warn" if pct >= 25 else "ok",
        })
    sectors.sort(key=lambda x: -x["pct"])

    # ── 壓力測試 ──
    stress = []
    for lv in STRESS_LEVELS:
        f = 1 + lv / 100
        sm = round(m_mv * f / m_loan * 100, 1) if m_loan > 0 else None
        below = []
        if m_loan > 0:
            below = sorted(
                [{"ticker": x["ticker"], "name": x["name"],
                  "maint": round(x["market_value"] * f / x["loan"] * 100, 1)}
                 for x in m_items],
                key=lambda y: y["maint"])
            below = [b for b in below if b["maint"] < 130][:3]
        stress.append({
            "index_change_pct": lv,
            "portfolio_value": round(total_mv * f),
            "equity": round(total_mv * f - total_loan),
            "maintenance_pct": sm,
            "status": ("斷頭風險" if sm is not None and sm < 120
                       else "追繳" if sm is not None and sm < 130
                       else "警戒" if sm is not None and sm < 160 else "安全"),
            "stocks_below_130": below,
        })

    # 距追繳還可以跌多少
    buffer_pct = None
    if maint and m_mv > 0:
        buffer_pct = round((1 - (m_loan * 1.30) / m_mv) * 100, 1)

    warnings = []
    if maint is not None and maint < 160:
        warnings.append(f"整戶維持率 {maint}%，低於 160% 警戒線")
    if buffer_pct is not None and buffer_pct < 15:
        warnings.append(f"距離追繳僅剩 {buffer_pct}% 跌幅緩衝")
    for s in sectors:
        if s["level"] == "danger":
            warnings.append(f"{s['sector']} 佔總部位 {s['pct']}%，集中度過高（同族群齊漲齊跌，分散效果有限）")
    if leverage and leverage >= 2:
        warnings.append(f"總槓桿 {leverage} 倍，反轉時損益放大 {leverage} 倍")

    return {
        "total_market_value": round(total_mv),
        "total_loan": round(total_loan),
        "total_equity": round(total_eq),
        "leverage": leverage,
        "margin_maintenance_pct": maint,
        "buffer_to_call_pct": buffer_pct,
        "maintenance_note": "整戶維持率＝融資部位總市值÷融資總借款。本值依你輸入的成數估算，"
                            "未計利息與費用，最終以券商對帳單為準。",
        "holdings": sorted(items, key=lambda x: -x["market_value"]),
        "sectors": sectors,
        "stress_test": stress,
        "stress_note": "假設個股與大盤同幅下跌；高 beta 股實際跌幅通常更大，此為樂觀估計。",
        "warnings": warnings,
        "checked_at": datetime.now(TW_TZ).isoformat(),
    }

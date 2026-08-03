from fastapi import APIRouter, BackgroundTasks
from database import db

router = APIRouter(prefix="/signals", tags=["signals"])

@router.get("/early")
async def early_signals():
    from services.signals import scan_early
    return await scan_early()

@router.get("/laggards")
async def laggards():
    from services.signals import scan_laggards
    return {"laggards": await scan_laggards()}

@router.get("/revenue")
async def revenue(user_id: str = ""):
    """月營收動能：universe = 35族群成分股 + 持倉 + 觀察清單"""
    from services.signals import revenue_momentum
    tickers = set()
    try:
        from routers.scan import TW_THEME_SECTORS
        for lst in TW_THEME_SECTORS.values():
            tickers.update(lst)
    except Exception:
        pass
    if user_id:
        async for h in db.holdings.find({"user_id": user_id, "market": "tw"}, {"ticker": 1}):
            tickers.add(h["ticker"])
        async for w in db.watchlist.find({"user_id": user_id, "market": "tw"}, {"ticker": 1}):
            tickers.add(w["ticker"])
    result = await revenue_momentum(sorted(tickers))
    return {"count": len(result), "universe": len(tickers), "items": result}

@router.get("/holders/{ticker}")
async def holders(ticker: str):
    from services.signals import holders_ratio
    return await holders_ratio(ticker)

@router.get("/backfill")
async def backfill(background_tasks: BackgroundTasks, days: int = 130):
    """觸發歷史回補（背景執行），用 /signals/status 看進度"""
    from services.snapshot import run_backfill, backfill_state
    st = backfill_state()
    if st["running"]:
        return {"status": "already_running", **st}
    background_tasks.add_task(run_backfill, days)
    return {"status": "started", "days": days, "check": "/signals/status"}

@router.get("/snapshot-now")
async def snapshot_now():
    from services.snapshot import run_daily_snapshot
    return await run_daily_snapshot()

@router.get("/status")
async def status():
    from services.snapshot import db_status
    return await db_status()

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from services.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        print(f"[Startup] 排程啟動失敗：{e}")
    try:
        from services.snapshot import ensure_indexes
        await ensure_indexes()
    except Exception as e:
        print(f"[Startup] index 失敗：{e}")
    yield

app = FastAPI(title="TradeGuard API v2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers.holdings    import router as holdings_router
from routers.scan        import router as scan_router
from routers.reviews     import router as reviews_router
from routers.trades      import router as trades_router
from routers.users       import router as users_router
from routers.stock       import router as stock_router
from routers.auth_router import router as auth_router
from routers.gooaye      import router as gooaye_router
from routers.market      import router as market_router
from routers.watchlist   import router as watchlist_router
from routers.signals     import router as signals_router
from routers.etf         import router as etf_router
from routers.chips       import router as chips_router

app.include_router(holdings_router)
app.include_router(scan_router)
app.include_router(reviews_router)
app.include_router(trades_router)
app.include_router(users_router)
app.include_router(stock_router)
app.include_router(auth_router)
app.include_router(gooaye_router)
app.include_router(market_router)
app.include_router(watchlist_router)
app.include_router(signals_router)
app.include_router(etf_router)
app.include_router(chips_router)

@app.get("/")
async def root():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return {"status": "ok", "message": "TradeGuard API v2 is running"}

@app.get("/status")
async def status():
    return {"status": "ok", "message": "TradeGuard API v2 is running"}

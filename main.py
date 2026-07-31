from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
import os

load_dotenv()

# FIX: @app.on_event("startup") 在 FastAPI 0.93+ 已 deprecated
#      改用 lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 移除啟動預載，避免 TWSE 連線 timeout 造成 Railway crash
    yield
    # shutdown 時可加清理邏輯（目前無需要）

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

app.include_router(holdings_router)
app.include_router(scan_router)
app.include_router(reviews_router)
app.include_router(trades_router)
app.include_router(users_router)
app.include_router(stock_router)
app.include_router(auth_router)
app.include_router(gooaye_router)

@app.get("/")
async def root():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return {"status": "ok", "message": "TradeGuard API v2 is running 🚀"}

@app.get("/status")
async def status():
    return {"status": "ok", "message": "TradeGuard API v2 is running 🚀"}

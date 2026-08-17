from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os

load_dotenv()

_startup_errors = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from services.scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        _startup_errors["scheduler"] = f"{type(e).__name__}: {e}"
        print(f"[Startup] 排程啟動失敗：{e}")
    try:
        from services.snapshot import ensure_indexes
        await ensure_indexes()
    except Exception as e:
        _startup_errors["indexes"] = f"{type(e).__name__}: {e}"
        print(f"[Startup] index 失敗：{e}")
    yield

app = FastAPI(title="TradeGuard API v2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由掛載（容錯：單一模組失敗不影響整個 App 啟動）──
_router_errors = {}

def _mount(module_name: str, attr: str = "router", required: bool = False):
    try:
        mod = __import__(f"routers.{module_name}", fromlist=[attr])
        app.include_router(getattr(mod, attr))
        print(f"[Router] OK  routers.{module_name}")
    except Exception as e:
        _router_errors[module_name] = f"{type(e).__name__}: {e}"
        print(f"[Router] FAIL routers.{module_name} -> {type(e).__name__}: {e}")
        if required:
            raise

# 核心（缺了就沒意義，仍容錯以便看到錯誤）
for _m in ("holdings", "scan", "reviews", "trades", "users", "stock"):
    _mount(_m)

_mount("auth_router")
_mount("gooaye")
_mount("market")
_mount("watchlist")
_mount("signals")
_mount("etf")
_mount("chips")
_mount("audit")
_mount("risk")
_mount("discipline")
_mount("backtest")
_mount("stability")


@app.get("/health")
async def health():
    """啟動健檢：哪些 router 掛載失敗、錯誤是什麼"""
    return {"status": "ok" if not (_router_errors or _startup_errors) else "degraded",
            "router_errors": _router_errors,
            "startup_errors": _startup_errors,
            "mounted": sorted({r.path for r in app.routes})}


@app.get("/")
async def root():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return {"status": "ok", "message": "TradeGuard API v2 is running"}

@app.get("/status")
async def status():
    return {"status": "ok", "message": "TradeGuard API v2 is running"}

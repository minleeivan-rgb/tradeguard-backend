from fastapi import APIRouter
from datetime import datetime
from database import db
from models import Trade

router = APIRouter(prefix="/trades", tags=["trades"])

def fix_id(doc):
    if doc and "_id" in doc:
        doc["_id"] = str(doc["_id"])
    return doc

@router.post("")
async def add_trade(trade: Trade):
    data = trade.dict()
    data["pnl_pct"] = round((trade.exit_price - trade.entry_price) / trade.entry_price * 100, 2)
    data["created_at"] = datetime.utcnow().isoformat()
    result = await db.trades.insert_one(data)
    return {"id": str(result.inserted_id), "pnl_pct": data["pnl_pct"]}

@router.get("/{user_id}")
async def get_trades(user_id: str):
    trades = []
    async for t in db.trades.find({"user_id": user_id}).sort("created_at", -1):
        trades.append(fix_id(t))
    return trades

@router.get("/{user_id}/stats")
async def get_trade_stats(user_id: str):
    trades = [t async for t in db.trades.find({"user_id": user_id})]
    if not trades:
        return {"total": 0, "win_rate": 0, "avg_profit": 0, "avg_loss": 0}
    total  = len(trades)
    wins   = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    return {
        "total": total,
        "win_rate":      round(len(wins) / total * 100, 1),
        "avg_profit":    round(sum(t["pnl_pct"] for t in wins) / len(wins), 1) if wins else 0,
        "avg_loss":      round(sum(t["pnl_pct"] for t in losses) / len(losses), 1) if losses else 0,
        "discipline_rate": round(sum(1 for t in trades if t.get("discipline")) / total * 100, 1)
    }

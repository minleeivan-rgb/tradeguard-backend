from pydantic import BaseModel
from typing import Optional, List

class Holding(BaseModel):
    user_id: str
    ticker: str
    name: str
    market: str
    entry_price: float
    entry_date: str
    shares: int
    unit: str = "share"
    margin: bool = False
    margin_ratio: float = 0.6
    story: str = ""

class HoldingUpdate(BaseModel):
    current_price: Optional[float] = None
    highest_price: Optional[float] = None
    status: Optional[str] = None
    story: Optional[str] = None
    note: Optional[str] = None

class NoteUpdate(BaseModel):
    note: str

class Review(BaseModel):
    user_id: str
    type: str
    ticker: str
    inputs: dict
    ai_response: str
    verdict: str

class AIReviewRequest(BaseModel):
    user_id: str
    type: str
    ticker: str
    market: str
    inputs: dict
    profit_pct: float = 20
    stoploss_pct: float = 7

class Trade(BaseModel):
    user_id: str
    ticker: str
    name: str
    market: str
    entry_price: float
    exit_price: float
    shares: int
    entry_date: str
    exit_date: str
    exit_reason: str
    discipline: bool

class UserRules(BaseModel):
    profit_trailing_pct: float = 20
    stoploss_pct: float = 7
    stoploss_ma: List[str] = ["monthly", "quarterly"]
    markets: List[str] = ["tw", "us"]
    strategy: str = "momentum_midlong"

class CustomRule(BaseModel):
    title: str
    content: str
    category: str = "general"

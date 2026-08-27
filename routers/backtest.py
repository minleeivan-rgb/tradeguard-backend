"""
回測引擎（查 market_daily 快照，不打外部 API）

設計原則（避免自我欺騙）：
1. 無未來函數：訊號用第 T 日收盤資料計算，第 T+1 日收盤才進場
2. 一律附「同期全市場基準報酬」，勝率高不等於有 edge，要贏過基準才算
3. 計入交易成本（手續費 0.1425%×折扣 進出各一次 + 賣出交易稅 0.3%）
4. 樣本數不足直接標示，不給沒有意義的百分比

- /backtest/entry   進場訊號驗證（vol_quiet / squeeze / high60 / trust）
- /backtest/exit    出場規則比較（移動停利 vs 破月線 vs 固定持有）
- /backtest/status  可回測期間
"""
from fastapi import APIRouter
import pandas as pd
from database import db

router = APIRouter(prefix="/backtest", tags=["backtest"])

HORIZONS = [5, 10, 20]
FEE_RATE = 0.001425 * 0.3          # 手續費（假設 3 折）
TAX_RATE = 0.003                   # 賣出交易稅
ROUND_TRIP_COST = FEE_RATE * 2 + TAX_RATE


async def _frames(max_days: int = 400):
    dates = sorted(await db.market_daily.distinct("date"))
    if len(dates) < 40:
        return None, None, dates
    use = dates[-max_days:]
    rows = [r async for r in db.market_daily.find(
        {"date": {"$in": use}},
        {"date": 1, "ticker": 1, "close": 1, "volume": 1, "_id": 0})]
    if not rows:
        return None, None, use
    df = pd.DataFrame(rows)
    close = df.pivot_table(index="date", columns="ticker", values="close").sort_index()
    vol   = df.pivot_table(index="date", columns="ticker", values="volume").sort_index()
    return close, vol, use


_NAME_MAP: dict = {}


async def _ensure_names():
    global _NAME_MAP
    try:
        from services.finmind import get_tw_name_map
        m = await get_tw_name_map()
        if m:
            _NAME_MAP = m
    except Exception:
        pass


def _name(t: str) -> str:
    t = str(t)
    if _NAME_MAP.get(t):
        return _NAME_MAP[t]
    try:
        from routers.scan import TW_STOCK_LIST
        return TW_STOCK_LIST.get(t, t)
    except Exception:
        return t


def _signal_mask(strategy: str, close: pd.DataFrame, vol: pd.DataFrame, i: int):
    """回傳第 i 天（收盤後）符合訊號的股票 index。只用 <= i 的資料。"""
    c_now, c_prev = close.iloc[i], close.iloc[i - 1]
    chg   = (c_now / c_prev - 1) * 100
    v_now = vol.iloc[i]
    v_ma20 = vol.iloc[i - 20:i].mean()
    vr = v_now / v_ma20.replace(0, float("nan"))
    liquid = (v_now >= 1_000_000) & (c_now >= 10)

    if strategy == "vol_quiet":
        m = liquid & (vr >= 2.5) & (chg >= 0) & (chg < 4)
    elif strategy == "squeeze":
        w = close.iloc[: i + 1]
        ma5  = w.rolling(5).mean().iloc[-2]
        ma10 = w.rolling(10).mean().iloc[-2]
        ma20 = w.rolling(20).mean().iloc[-2]
        band = pd.concat([ma5, ma10, ma20], axis=1)
        spread = (band.max(axis=1) - band.min(axis=1)) / band.min(axis=1) * 100
        cur_band = pd.concat([w.rolling(5).mean().iloc[-1],
                              w.rolling(10).mean().iloc[-1],
                              w.rolling(20).mean().iloc[-1]], axis=1).max(axis=1)
        m = liquid & (spread < 3) & (c_now > cur_band) & (chg >= 2) & (vr >= 1.5)
    elif strategy == "high60":
        lookback = close.iloc[max(0, i - 60): i]
        m = liquid & (c_now >= lookback.max()) & (vr >= 1.3) & (chg >= 1)
    elif strategy == "ma20_cross":
        w = close.iloc[: i + 1]
        ma20 = w.rolling(20).mean()
        m = liquid & (c_now > ma20.iloc[-1]) & (c_prev <= ma20.iloc[-2])
    else:
        return None
    return m.fillna(False)


@router.get("/status")
async def status():
    dates = sorted(await db.market_daily.distinct("date"))
    return {"days": len(dates),
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "ready": len(dates) >= 60,
            "note": "建議至少 60 個交易日；不足時請先執行 /signals/backfill"}


@router.get("/entry")
async def backtest_entry(strategy: str = "vol_quiet", max_per_day: int = 20):
    """進場訊號驗證：訊號日 T 收盤觸發 → T+1 收盤買進 → 持有 N 日"""
    await _ensure_names()
    close, vol, dates = await _frames()
    if close is None:
        return {"error": f"歷史資料不足（{len(dates)} 天），請先執行 /signals/backfill"}
    if strategy not in ("vol_quiet", "squeeze", "high60", "ma20_cross"):
        return {"error": "strategy 需為 vol_quiet / squeeze / high60 / ma20_cross"}

    n = len(close)
    max_h = max(HORIZONS)
    trades = []
    for i in range(21, n - max_h - 1):
        mask = _signal_mask(strategy, close, vol, i)
        if mask is None or not mask.any():
            continue
        picks = list(close.columns[mask])[:max_per_day]
        entry_i = i + 1                                # T+1 收盤進場，無未來函數
        for t in picks:
            ep = close.iloc[entry_i].get(t)
            if not ep or pd.isna(ep) or ep <= 0:
                continue
            rec = {"signal_date": close.index[i], "entry_date": close.index[entry_i],
                   "ticker": t, "entry": round(float(ep), 2)}
            for hz in HORIZONS:
                xi = entry_i + hz
                if xi >= n:
                    continue
                xp = close.iloc[xi].get(t)
                if xp and not pd.isna(xp):
                    gross = (float(xp) / float(ep) - 1) * 100
                    rec[f"d{hz}"] = round(gross - ROUND_TRIP_COST * 100, 2)
            trades.append(rec)

    if not trades:
        return {"strategy": strategy, "error": "此區間無訊號觸發", "days_tested": n}

    # 基準：同期全市場等權平均（相同進場日與持有期）
    baseline = {}
    for hz in HORIZONS:
        rets = []
        for i in range(22, n - hz):
            a, b = close.iloc[i], close.iloc[i + hz]
            r = ((b / a - 1) * 100).dropna()
            if len(r):
                rets.append(r.mean())
        baseline[f"d{hz}"] = round(sum(rets) / len(rets), 2) if rets else None

    out = {"strategy": strategy, "trades": len(trades),
           "period": f"{close.index[0]} ~ {close.index[-1]}",
           "days_tested": n, "cost_assumption_pct": round(ROUND_TRIP_COST * 100, 3),
           "results": {}, "baseline_market_avg": baseline}

    for hz in HORIZONS:
        key = f"d{hz}"
        vals = [t[key] for t in trades if key in t]
        if len(vals) < 5:
            out["results"][key] = {"n": len(vals), "note": "樣本不足，不計算統計值"}
            continue
        s = pd.Series(vals)
        wins = s[s > 0]
        losses = s[s <= 0]
        edge = round(s.mean() - baseline[key], 2) if baseline.get(key) is not None else None
        out["results"][key] = {
            "n": len(vals),
            "win_rate_pct": round(len(wins) / len(vals) * 100, 1),
            "avg_return_pct": round(s.mean(), 2),
            "median_return_pct": round(s.median(), 2),
            "avg_win_pct": round(wins.mean(), 2) if len(wins) else None,
            "avg_loss_pct": round(losses.mean(), 2) if len(losses) else None,
            "best_pct": round(s.max(), 2), "worst_pct": round(s.min(), 2),
            "edge_vs_market_pct": edge,
            "verdict": ("有正向 edge" if edge is not None and edge > 0.5
                        else "無明顯 edge" if edge is not None and edge > -0.5
                        else "劣於大盤"),
        }

    best = sorted(trades, key=lambda x: -(x.get("d10") or -99))[:5]
    worst = sorted(trades, key=lambda x: (x.get("d10") or 99))[:5]
    out["sample_best"] = [{**b, "name": _name(b["ticker"])} for b in best]
    out["sample_worst"] = [{**b, "name": _name(b["ticker"])} for b in worst]
    out["warning"] = ("回測結果不保證未來表現。樣本期間偏短時，結果易受單一行情主導；"
                      "edge 小於 1% 者實務上會被滑價吃掉。")
    return out


@router.get("/exit")
async def backtest_exit(strategy: str = "high60", trailing_pct: float = 20,
                        stoploss_pct: float = 7, max_hold: int = 60,
                        max_per_day: int = 10):
    """出場規則比較：同一批進場訊號，套用三種出場規則看總報酬差異"""
    close, vol, dates = await _frames()
    if close is None:
        return {"error": f"歷史資料不足（{len(dates)} 天），請先執行 /signals/backfill"}

    n = len(close)
    ma20_all = close.rolling(20).mean()
    entries = []
    for i in range(21, n - 10):
        mask = _signal_mask(strategy, close, vol, i)
        if mask is None or not mask.any():
            continue
        for t in list(close.columns[mask])[:max_per_day]:
            ep = close.iloc[i + 1].get(t)
            if ep and not pd.isna(ep) and ep > 0:
                entries.append((i + 1, t, float(ep)))

    if not entries:
        return {"strategy": strategy, "error": "此區間無訊號觸發"}

    rules = {
        f"移動停利{trailing_pct:g}%+停損{stoploss_pct:g}%": "trailing",
        "跌破月線出場": "ma20",
        "固定持有20日": "hold20",
    }
    results = {}
    for label, kind in rules.items():
        rets, holds = [], []
        for ei, t, ep in entries:
            peak = ep
            exit_p, held = None, 0
            for j in range(ei + 1, min(ei + 1 + max_hold, n)):
                px = close.iloc[j].get(t)
                if px is None or pd.isna(px):
                    continue
                px = float(px)
                held = j - ei
                peak = max(peak, px)
                if kind == "trailing":
                    if (px - ep) / ep * 100 <= -stoploss_pct:
                        exit_p = px; break
                    if px > ep and (peak - px) / peak * 100 >= trailing_pct:
                        exit_p = px; break
                elif kind == "ma20":
                    m = ma20_all.iloc[j].get(t)
                    if m and not pd.isna(m) and px < float(m):
                        exit_p = px; break
                elif kind == "hold20":
                    if held >= 20:
                        exit_p = px; break
            if exit_p is None:
                last_i = min(ei + max_hold, n - 1)
                px = close.iloc[last_i].get(t)
                if px is None or pd.isna(px):
                    continue
                exit_p, held = float(px), last_i - ei
            rets.append((exit_p / ep - 1) * 100 - ROUND_TRIP_COST * 100)
            holds.append(held)

        if len(rets) < 5:
            results[label] = {"n": len(rets), "note": "樣本不足"}
            continue
        s = pd.Series(rets)
        wins = s[s > 0]
        # 簡易最大回撤（等權序列累積）
        eq = (1 + s / 100).cumprod()
        dd = round(float(((eq - eq.cummax()) / eq.cummax()).min() * 100), 2)
        results[label] = {
            "n": len(rets),
            "win_rate_pct": round(len(wins) / len(s) * 100, 1),
            "avg_return_pct": round(s.mean(), 2),
            "total_return_pct": round((eq.iloc[-1] - 1) * 100, 2),
            "avg_hold_days": round(sum(holds) / len(holds), 1),
            "worst_trade_pct": round(s.min(), 2),
            "max_drawdown_pct": dd,
        }

    valid = {k: v for k, v in results.items() if "avg_return_pct" in v}
    best = max(valid, key=lambda k: valid[k]["avg_return_pct"]) if valid else None
    return {
        "strategy": strategy, "entries": len(entries),
        "period": f"{close.index[0]} ~ {close.index[-1]}",
        "params": {"trailing_pct": trailing_pct, "stoploss_pct": stoploss_pct,
                   "max_hold_days": max_hold},
        "results": results, "best_by_avg_return": best,
        "warning": "出場規則的優劣高度取決於樣本期間的行情型態（多頭/震盪/空頭），"
                   "單一期間的結論不可外推；建議累積更多歷史後重跑。",
    }

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


LIMIT_MOVE = 0.11      # 台股單日漲跌幅上限 10%，超過視為除權息/減資等公司行動


def _corp_action_cum(close: pd.DataFrame):
    """回傳累積計數表：用來 O(1) 判斷某段期間內是否發生公司行動（未還原股價跳空）"""
    bad = close.pct_change().abs() > LIMIT_MOVE
    return bad.cumsum(), int(bad.values.sum())


def _has_corp_action(cum, a: int, b: int, ticker: str) -> bool:
    """(a, b] 區間內該股是否有公司行動"""
    try:
        return bool(cum.iloc[b].get(ticker, 0) - cum.iloc[a].get(ticker, 0) > 0)
    except Exception:
        return False


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
    cum, total_bad = _corp_action_cum(close)
    trades = []
    excluded = 0
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
            # 進場當日本身若為公司行動跳空，該筆不採用
            if _has_corp_action(cum, entry_i - 1, entry_i, t):
                excluded += 1
                continue
            rec = {"signal_date": close.index[i], "entry_date": close.index[entry_i],
                   "ticker": t, "entry": round(float(ep), 2)}
            skipped_any = False
            for hz in HORIZONS:
                xi = entry_i + hz
                if xi >= n:
                    continue
                if _has_corp_action(cum, entry_i, xi, t):
                    skipped_any = True          # 持有期間除權息/減資，報酬失真，不列入
                    continue
                xp = close.iloc[xi].get(t)
                if xp and not pd.isna(xp):
                    gross = (float(xp) / float(ep) - 1) * 100
                    rec[f"d{hz}"] = round(gross - ROUND_TRIP_COST * 100, 2)
            if skipped_any and not any(f"d{h}" in rec for h in HORIZONS):
                excluded += 1
                continue
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
           "excluded_corporate_actions": excluded,
           "corp_action_days_in_data": total_bad,
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

    with_d10 = [t for t in trades if t.get("d10") is not None]
    best  = sorted(with_d10, key=lambda x: -x["d10"])[:8]
    worst = sorted(with_d10, key=lambda x: x["d10"])[:8]
    recent = sorted(trades, key=lambda x: x["signal_date"], reverse=True)[:20]
    out["sample_best"]  = [{**b, "name": _name(b["ticker"])} for b in best]
    out["sample_worst"] = [{**b, "name": _name(b["ticker"])} for b in worst]
    out["recent_signals"] = [{**b, "name": _name(b["ticker"])} for b in recent]
    out["warning"] = ("回測結果不保證未來表現。樣本期間偏短時，結果易受單一行情主導；"
                      "edge 小於 1% 者實務上會被滑價吃掉。"
                      "已排除單日漲跌超過 11% 的公司行動（除權息／減資）造成的失真。")
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
    cum, _total_bad = _corp_action_cum(close)
    entries = []
    excluded = 0
    for i in range(21, n - 10):
        mask = _signal_mask(strategy, close, vol, i)
        if mask is None or not mask.any():
            continue
        for t in list(close.columns[mask])[:max_per_day]:
            ep = close.iloc[i + 1].get(t)
            if ep and not pd.isna(ep) and ep > 0:
                if _has_corp_action(cum, i, i + 1, t):
                    excluded += 1
                    continue
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
        rets, holds, detail = [], [], []
        for ei, t, ep in entries:
            peak = ep
            exit_p, held, exit_i = None, 0, None
            corp = False
            for j in range(ei + 1, min(ei + 1 + max_hold, n)):
                px = close.iloc[j].get(t)
                if px is None or pd.isna(px):
                    continue
                if _has_corp_action(cum, j - 1, j, t):
                    corp = True          # 持有期間除權息／減資，該筆不計
                    break
                px = float(px)
                held = j - ei
                peak = max(peak, px)
                if kind == "trailing":
                    if (px - ep) / ep * 100 <= -stoploss_pct:
                        exit_p, exit_i = px, j; break
                    if px > ep and (peak - px) / peak * 100 >= trailing_pct:
                        exit_p, exit_i = px, j; break
                elif kind == "ma20":
                    m = ma20_all.iloc[j].get(t)
                    if m and not pd.isna(m) and px < float(m):
                        exit_p, exit_i = px, j; break
                elif kind == "hold20":
                    if held >= 20:
                        exit_p, exit_i = px, j; break
            if corp:
                continue
            if exit_p is None:
                last_i = min(ei + max_hold, n - 1)
                px = close.iloc[last_i].get(t)
                if px is None or pd.isna(px):
                    continue
                exit_p, held, exit_i = float(px), last_i - ei, last_i
            r = (exit_p / ep - 1) * 100 - ROUND_TRIP_COST * 100
            rets.append(r)
            holds.append(held)
            detail.append({"entry_date": close.index[ei], "ticker": t,
                           "name": _name(t), "entry": round(ep, 2),
                           "exit_date": close.index[exit_i], "exit": round(exit_p, 2),
                           "hold_days": held, "return_pct": round(r, 2)})

        if len(rets) < 5:
            results[label] = {"n": len(rets), "note": "樣本不足"}
            continue
        s = pd.Series(rets)
        wins = s[s > 0]

        # 最大回撤：以「訊號日等權買進當日全部候選」建構日期序列權益曲線
        by_date = {}
        for d in detail:
            by_date.setdefault(d["entry_date"], []).append(d["return_pct"])
        period_rets = [sum(v) / len(v) for _, v in sorted(by_date.items())]
        eq = (1 + pd.Series(period_rets) / 100).cumprod()
        dd = round(float(((eq - eq.cummax()) / eq.cummax()).min() * 100), 2) if len(eq) else None

        detail.sort(key=lambda x: x["return_pct"])
        results[label] = {
            "n": len(rets),
            "win_rate_pct": round(len(wins) / len(s) * 100, 1),
            "avg_return_pct": round(s.mean(), 2),
            "median_return_pct": round(s.median(), 2),
            "avg_hold_days": round(sum(holds) / len(holds), 1),
            "worst_trade_pct": round(s.min(), 2),
            "best_trade_pct": round(s.max(), 2),
            "max_drawdown_pct": dd,
            "periods": len(period_rets),
            "sample_worst": detail[:5],
            "sample_best": list(reversed(detail[-5:])),
        }

    valid = {k: v for k, v in results.items() if "avg_return_pct" in v}
    best = max(valid, key=lambda k: valid[k]["avg_return_pct"]) if valid else None
    return {
        "strategy": strategy, "entries": len(entries),
        "excluded_corporate_actions": excluded,
        "period": f"{close.index[0]} ~ {close.index[-1]}",
        "params": {"trailing_pct": trailing_pct, "stoploss_pct": stoploss_pct,
                   "max_hold_days": max_hold},
        "results": results, "best_by_avg_return": best,
        "drawdown_note": "最大回撤以「每個訊號日等權買進當日全部候選」的日期序列權益曲線計算，"
                         "非單筆連續全押",
        "warning": "出場規則的優劣高度取決於樣本期間的行情型態（多頭/震盪/空頭），"
                   "單一期間的結論不可外推；建議累積更多歷史後重跑。"
                   "已排除除權息／減資造成的價格跳空。",
    }

"""
籌碼安定度評分（stability score 0-100）

五個成分，每一項都回傳「這代表什麼結果」而不只是數字：
  1. 大戶持股比率變化   權重 25
  2. 股東人數變化       權重 20
  3. 當沖比率           權重 20
  4. 融資使用率         權重 20
  5. 分點買超集中度     權重 15

分數高 = 籌碼安定（持有者抱得久）→ 可用較緊的停損、部位可大
分數低 = 籌碼浮動（短線客多）→ 停損要放寬或部位縮小，否則會被洗掉

重要限制（顯示在回傳裡，不要忽略）：
- 集保股權分散表是「週資料」，週五盤後才更新上週五，滯後 5 個交易日以上
- 安定度高不代表會漲；流通籌碼少的股票下殺時同樣沒人接
- 這是「波動性質」的量化，不是買賣訊號
"""
import os
import asyncio
from fastapi import APIRouter
from datetime import datetime, timedelta, timezone
import httpx

router = APIRouter(prefix="/stability", tags=["stability"])

FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
FM = "https://api.finmindtrade.com/api/v4/data"
TW_TZ = timezone(timedelta(hours=8))


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


async def _fm(dataset: str, data_id: str = None, days: int = 90) -> list:
    start = (datetime.now(TW_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
    end = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    p = {"dataset": dataset, "start_date": start, "end_date": end, "token": FINMIND_TOKEN}
    if data_id:
        p["data_id"] = data_id
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(FM, params=p)
    j = r.json()
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        return j["data"]
    raise Exception(str(j)[:180])


def _level_low(lvl: str) -> int:
    digits = "".join(ch for ch in str(lvl).split("-")[0] if ch.isdigit())
    try:
        return int(digits) if digits else 0
    except Exception:
        return 0


# ── 1. 大戶持股比率 ──────────────────────────────────────────────
async def _big_holders(ticker: str) -> dict:
    rows = await _fm("TaiwanStockHoldingSharesPer", ticker, days=90)
    if not rows:
        return {"error": "無集保資料"}
    big, people, total_holders = {}, {}, {}
    for x in rows:
        d = x["date"]
        lvl = str(x.get("HoldingSharesLevel", ""))
        pct = float(x.get("percent", 0) or 0)
        cnt = float(x.get("people", 0) or 0)
        if lvl.lower().startswith("total") or "合計" in lvl:
            total_holders[d] = cnt
            continue
        people[d] = people.get(d, 0) + cnt
        if _level_low(lvl) >= 400001 or "1,000,001" in lvl or "more" in lvl.lower():
            big[d] = big.get(d, 0) + pct
    dates = sorted(big)
    if len(dates) < 2:
        return {"error": "集保資料期數不足"}
    series = [{"date": d, "pct": round(big[d], 2)} for d in dates[-6:]]
    delta = round(series[-1]["pct"] - series[0]["pct"], 2)

    # 股東人數
    hp = total_holders or people
    hdates = sorted(hp)
    holders_delta_pct = None
    if len(hdates) >= 2:
        a, b = hp[hdates[0]], hp[hdates[-1]]
        if a > 0:
            holders_delta_pct = round((b - a) / a * 100, 2)

    return {"series": series, "latest_pct": series[-1]["pct"], "delta": delta,
            "holders_latest": hp[hdates[-1]] if hdates else None,
            "holders_delta_pct": holders_delta_pct,
            "data_date": dates[-1]}


# ── 2. 當沖比率 ─────────────────────────────────────────────────
async def _day_trading(ticker: str) -> dict:
    rows = await _fm("TaiwanStockDayTrading", ticker, days=30)
    if not rows:
        return {"error": "無當沖資料"}
    rows.sort(key=lambda x: x["date"])
    recent = rows[-10:]
    vals = []
    for r in recent:
        vol = float(r.get("Volume", 0) or 0)
        dt_vol = float(r.get("BuyAfterSalesVolume", 0) or 0) \
            + float(r.get("SellAfterBuyVolume", 0) or 0)
        if vol > 0:
            vals.append(min(dt_vol / vol * 100, 100))
    if not vals:
        return {"error": "當沖欄位無法計算"}
    avg = round(sum(vals) / len(vals), 1)
    return {"avg_pct_10d": avg, "days": len(vals), "data_date": recent[-1]["date"]}


# ── 3. 融資使用率 ───────────────────────────────────────────────
async def _margin_usage(ticker: str) -> dict:
    rows = await _fm("TaiwanStockMarginPurchaseShortSale", ticker, days=40)
    if not rows:
        return {"error": "無融資資料"}
    rows.sort(key=lambda x: x["date"])
    last = rows[-1]
    bal = float(last.get("MarginPurchaseTodayBalance", 0) or 0)
    limit = float(last.get("MarginPurchaseLimit", 0) or 0)
    usage = round(bal / limit * 100, 2) if limit > 0 else None
    prev5 = rows[-6] if len(rows) >= 6 else rows[0]
    b5 = float(prev5.get("MarginPurchaseTodayBalance", 0) or 0)
    chg5 = round((bal - b5) / b5 * 100, 2) if b5 > 0 else None
    return {"balance_lots": round(bal / 1000), "usage_pct": usage,
            "change_5d_pct": chg5, "data_date": last["date"]}


# ── 4. 分點集中度 ───────────────────────────────────────────────
async def _branch_conc(ticker: str) -> dict:
    try:
        from routers.chips import branch_chips
        r = await branch_chips(ticker)
        if r.get("error"):
            return {"error": r["error"]}
        return {"conc_pct": r.get("buy_concentration_pct"),
                "streak_buyers": len(r.get("streak_buyers") or []),
                "data_date": r.get("date")}
    except Exception as e:
        return {"error": str(e)}


# ── 評分 ────────────────────────────────────────────────────────
def _score_big(d: dict):
    if d.get("error"):
        return None, d["error"], None
    delta = d["delta"]
    if delta >= 1.0:
        s = 95
        c = f"大戶比率近月增 {delta}%（現 {d['latest_pct']}%）→ 有人在收，浮額變少，回檔時比較有支撐，波段抱單容錯較高"
    elif delta >= 0.3:
        s = 78
        c = f"大戶比率微增 {delta}%（現 {d['latest_pct']}%）→ 溫和吸籌，尚未形成明顯鎖倉"
    elif delta > -0.3:
        s = 55
        c = f"大戶比率持平（現 {d['latest_pct']}%）→ 沒有明確方向，籌碼結構中性"
    elif delta > -1.0:
        s = 32
        c = f"大戶比率減 {abs(delta)}%（現 {d['latest_pct']}%）→ 大戶在減，散戶接手，上漲會遇到獲利賣壓"
    else:
        s = 12
        c = (f"大戶比率大減 {abs(delta)}%（現 {d['latest_pct']}%）→ 明顯派貨，"
             f"這種結構下追高風險高，反彈易無量")
    return s, c, d["data_date"]


def _score_holders(d: dict):
    if d.get("error") or d.get("holders_delta_pct") is None:
        return None, "股東人數資料不足", None
    hd = d["holders_delta_pct"]
    if hd <= -5:
        s = 92
        c = f"股東人數減 {abs(hd)}% → 人數變少而籌碼未散，典型集中過程，籌碼被少數人鎖住"
    elif hd <= -1:
        s = 74
        c = f"股東人數減 {abs(hd)}% → 緩步集中中"
    elif hd < 3:
        s = 55
        c = f"股東人數變化 {hd:+}% → 持有結構穩定，無明顯集中或分散"
    elif hd < 10:
        s = 32
        c = f"股東人數增 {hd}% → 散戶在進場，籌碼變零碎，洗盤幅度會加大"
    else:
        s = 12
        c = (f"股東人數暴增 {hd}% → 大量新散戶進場，這通常出現在題材熱炒末期，"
             f"之後的震盪會非常劇烈")
    return s, c, d.get("data_date")


def _score_daytrade(d: dict):
    if d.get("error"):
        return None, d["error"], None
    v = d["avg_pct_10d"]
    if v < 10:
        s = 92
        c = f"當沖比 {v}% → 幾乎沒有隔日沖，價格由真實買賣決定，突破的可信度高"
    elif v < 20:
        s = 72
        c = f"當沖比 {v}% → 正常水準，短線客有限"
    elif v < 30:
        s = 48
        c = f"當沖比 {v}% → 短線交易偏多，日內容易假突破，進場點要等收盤確認"
    elif v < 45:
        s = 25
        c = (f"當沖比 {v}% → 高度當沖股，盤中上下影線長、洗盤兇，"
             f"停損設太緊會被無意義掃掉")
    else:
        s = 8
        c = (f"當沖比 {v}% → 極端當沖，價格幾乎由日內籌碼決定，"
             f"技術訊號失效率高，不建議用短均線當進出依據")
    return s, c, d.get("data_date")


def _score_margin(d: dict):
    if d.get("error"):
        return None, d["error"], None
    u, chg = d.get("usage_pct"), d.get("change_5d_pct")
    if u is None:
        return None, "無融資限額資料，無法計算使用率", d.get("data_date")
    if u < 5:
        s = 90
        c = f"融資使用率 {u}% → 槓桿浮額極少，下跌時沒有連鎖斷頭壓力"
    elif u < 12:
        s = 70
        c = f"融資使用率 {u}% → 槓桿部位可控"
    elif u < 20:
        s = 45
        c = f"融資使用率 {u}% → 融資偏多，急跌時會有追繳賣壓加速下殺"
    else:
        s = 18
        c = (f"融資使用率 {u}% → 槓桿沉重，一旦跌破關鍵均線容易觸發連環斷頭，"
             f"跌幅常超出基本面該有的幅度")
    if chg is not None and chg > 8:
        s = max(5, s - 15)
        c += f"；且融資 5 日暴增 {chg}%，散戶追價明顯，風險再加一層"
    return s, c, d.get("data_date")


def _score_branch(d: dict):
    if d.get("error") or d.get("conc_pct") is None:
        return None, "分點資料不足（每日約 21:00 後更新）", None
    v = d["conc_pct"]
    sb = d.get("streak_buyers", 0)
    if v >= 70:
        s = 88
        c = f"買超集中度 {v}% → 買盤集中在少數分點，有明確主力在主導方向"
    elif v >= 55:
        s = 68
        c = f"買超集中度 {v}% → 中度集中，有主力但也有一般買盤"
    elif v >= 40:
        s = 45
        c = f"買超集中度 {v}% → 買盤分散，多頭缺乏主導者，漲勢容易斷"
    else:
        s = 22
        c = f"買超集中度 {v}% → 買盤極度分散，典型散戶盤，走勢缺乏延續性"
    if sb >= 3:
        s = min(100, s + 8)
        c += f"；有 {sb} 家分點近 5 日連續買超"
    return s, c, d.get("data_date")


@router.get("/{ticker}")
async def stability(ticker: str):
    await _ensure_names()
    big, dt, mg, br = await asyncio.gather(
        _big_holders(ticker), _day_trading(ticker),
        _margin_usage(ticker), _branch_conc(ticker),
        return_exceptions=True)

    def _safe(x):
        return x if isinstance(x, dict) else {"error": str(x)[:150]}

    big, dt, mg, br = _safe(big), _safe(dt), _safe(mg), _safe(br)

    comps = []
    for label, weight, fn, raw in [
        ("大戶持股比率", 25, _score_big, big),
        ("股東人數變化", 20, _score_holders, big),
        ("當沖比率", 20, _score_daytrade, dt),
        ("融資使用率", 20, _score_margin, mg),
        ("分點集中度", 15, _score_branch, br),
    ]:
        s, consequence, ddate = fn(raw)
        comps.append({"name": label, "weight": weight, "score": s,
                      "consequence": consequence, "data_date": ddate})

    valid = [c for c in comps if c["score"] is not None]
    if not valid:
        return {"ticker": ticker, "name": _name(ticker),
                "error": "五項指標皆無資料", "components": comps}

    tw = sum(c["weight"] for c in valid)
    total = round(sum(c["score"] * c["weight"] for c in valid) / tw)

    if total >= 70:
        grade, gtxt = "A", "籌碼安定"
        action = ("持有者抱得久、洗盤淺。停損可依技術位置設定（例如破月線），"
                  "不需要為了雜訊放寬；部位可以是你的正常尺寸。")
    elif total >= 50:
        grade, gtxt = "B", "中性"
        action = ("結構普通。照既有規則操作即可，但進場點盡量等收盤確認，"
                  "不要用盤中跳動決定。")
    elif total >= 35:
        grade, gtxt = "C", "籌碼偏浮動"
        action = ("短線客多、假突破機率高。停損要比平常寬一到兩個百分點，"
                  "否則會被洗掉；相對地部位要縮小，讓放寬的停損不至於放大絕對虧損。")
    else:
        grade, gtxt = "D", "籌碼混亂"
        action = ("價格主要由日內籌碼決定，短均線訊號可信度低。"
                  "建議用更小的部位、更寬的停損，或乾脆只做明確帶量突破後的順勢單；"
                  "不適合重倉或加融資。")

    weakest = sorted(valid, key=lambda x: x["score"])[:2]

    return {
        "ticker": ticker, "name": _name(ticker),
        "score": total, "grade": grade, "grade_text": gtxt,
        "what_this_means": action,
        "components": comps,
        "weakest_links": [{"name": w["name"], "consequence": w["consequence"]} for w in weakest],
        "coverage": f"{len(valid)}/5 項有資料",
        "raw": {"holders": big, "day_trading": dt, "margin": mg, "branch": br},
        "limitations": [
            "集保股權分散表為週資料，週五盤後才更新，滯後 5 個交易日以上",
            "安定度高不代表會漲；流通籌碼少的股票下殺時同樣沒人接",
            "這是波動性質的量化，用來調整停損寬度與部位大小，不是買賣訊號",
            "評分區間與權重是本系統設定的規則，非市場公認標準",
        ],
        "checked_at": datetime.now(TW_TZ).isoformat(),
    }


@router.get("/batch/{user_id}")
async def stability_batch(user_id: str, source: str = "holdings"):
    """持倉或觀察清單的批次評分（逐檔查詢，會花 10-40 秒）"""
    from database import db
    coll = db.watchlist if source == "watchlist" else db.holdings
    tickers = []
    async for h in coll.find({"user_id": user_id, "market": "tw"}, {"ticker": 1}):
        if h["ticker"] not in tickers:
            tickers.append(h["ticker"])
    if not tickers:
        return {"items": [], "note": f"{source} 無台股標的"}

    items = []
    for t in tickers:
        try:
            r = await stability(t)
            items.append({"ticker": t, "name": r.get("name", t),
                          "score": r.get("score"), "grade": r.get("grade"),
                          "grade_text": r.get("grade_text"),
                          "what_this_means": r.get("what_this_means"),
                          "weakest_links": r.get("weakest_links", []),
                          "coverage": r.get("coverage"),
                          "error": r.get("error")})
        except Exception as e:
            items.append({"ticker": t, "name": _name(t), "error": str(e)[:120]})
        await asyncio.sleep(0.3)

    scored = [x for x in items if x.get("score") is not None]
    scored.sort(key=lambda x: x["score"])
    return {"source": source, "items": scored + [x for x in items if x.get("score") is None],
            "note": "分數低者排前面（最需要放寬停損／縮小部位的標的）"}

# ── 股東人數與級距分布（專屬端點）────────────────────────────────

RETAIL_MAX = 5000          # 5 張以下視為小額
BIG_MIN    = 400001        # 400 張以上視為大戶
WHALE_MIN  = 1000001       # 1000 張以上視為千張大戶


@router.get("/holders/{ticker}")
async def holders_detail(ticker: str, weeks: int = 12):
    """股東人數、平均持股、級距分布與變化（集保週資料）"""
    try:
        await _ensure_names()
        rows = await _fm("TaiwanStockHoldingSharesPer", ticker, days=weeks * 7 + 20)
        if not rows:
            return {"error": "無集保資料"}

        by_date = {}
        for x in rows:
            d = x["date"]
            lvl = str(x.get("HoldingSharesLevel", ""))
            low = _level_low(lvl)
            people = float(x.get("people", 0) or 0)
            shares = float(x.get("unit", 0) or 0)
            pct = float(x.get("percent", 0) or 0)
            low_l = lvl.lower()
            is_total = low_l.startswith("total") or "合計" in lvl or low_l.startswith("all")
            is_adj = "差異" in lvl or "adjust" in low_l
            b = by_date.setdefault(d, {"total_people": 0.0, "total_shares": 0.0,
                                       "sum_people": 0.0, "sum_shares": 0.0,
                                       "retail_people": 0.0, "big_pct": 0.0,
                                       "whale_people": 0.0, "whale_pct": 0.0,
                                       "levels": []})
            if is_total:
                b["total_people"] = people
                b["total_shares"] = shares
                continue
            if is_adj:
                continue
            b["sum_people"] += people
            b["sum_shares"] += shares
            b["levels"].append({"level": lvl, "people": people,
                                "shares": shares, "pct": round(pct, 2)})
            if low and low <= RETAIL_MAX:
                b["retail_people"] += people
            if low >= BIG_MIN:
                b["big_pct"] += pct
            if low >= WHALE_MIN:
                b["whale_people"] += people
                b["whale_pct"] += pct

        dates = sorted(by_date)
        if not dates:
            return {"error": "資料無法解析"}

        series = []
        for d in dates[-weeks:]:
            b = by_date[d]
            people = b["total_people"] or b["sum_people"]
            shares = b["total_shares"] or b["sum_shares"]
            series.append({
                "date": d,
                "holders": int(people),
                "avg_lots_per_holder": round(shares / people / 1000, 2) if people else None,
                "big_400_pct": round(b["big_pct"], 2),
                "whale_1000_people": int(b["whale_people"]),
                "whale_1000_pct": round(b["whale_pct"], 2),
                "retail_people_pct": round(b["retail_people"] / people * 100, 1) if people else None,
            })

        latest, first = series[-1], series[0]
        prev = series[-2] if len(series) > 1 else first

        def _chg(a, b):
            if a is None or b is None or b == 0:
                return None
            return round((a - b) / b * 100, 2)

        holders_wow = _chg(latest["holders"], prev["holders"])
        holders_period = _chg(latest["holders"], first["holders"])
        avg_period = _chg(latest["avg_lots_per_holder"], first["avg_lots_per_holder"])
        big_delta = round(latest["big_400_pct"] - first["big_400_pct"], 2)
        whale_delta = round(latest["whale_1000_pct"] - first["whale_1000_pct"], 2)

        # 判讀：人數↓ + 平均持股↑ = 集中；人數↑ + 平均持股↓ = 分散
        if holders_period is not None and avg_period is not None:
            if holders_period <= -3 and avg_period >= 2:
                verdict = "籌碼集中中"
                meaning = (f"{weeks} 週內股東人數減 {abs(holders_period)}%、平均每人持股增 {avg_period}% → "
                           f"小股東退出、籌碼往大戶集中。這種結構回檔時較有支撐，"
                           f"停損可依技術位置設定，不需為雜訊放寬。")
            elif holders_period >= 5 and avg_period <= -2:
                verdict = "籌碼分散中"
                meaning = (f"{weeks} 週內股東人數增 {holders_period}%、平均每人持股減 {abs(avg_period)}% → "
                           f"大量新散戶進場、籌碼變零碎。洗盤幅度會加大，"
                           f"停損設太緊容易被掃掉，部位要相應縮小。")
            elif holders_period >= 5 and big_delta < -0.5:
                verdict = "大戶出貨給散戶"
                meaning = (f"股東人數增 {holders_period}% 但 400 張以上比重減 {abs(big_delta)}% → "
                           f"典型派貨結構，上漲會遇到獲利賣壓，追高風險高。")
            else:
                verdict = "無明顯方向"
                meaning = (f"{weeks} 週內人數變化 {holders_period:+}%、平均持股 {avg_period:+}% → "
                           f"籌碼結構穩定，沒有明顯集中或分散，照既有規則操作即可。")
        else:
            verdict, meaning = "資料不足", "期數不足，無法判讀趨勢"

        level_dist = sorted(by_date[dates[-1]]["levels"],
                            key=lambda x: -x["pct"])[:16]

        return {
            "ticker": ticker, "name": _name(ticker),
            "data_date": latest["date"], "weeks_covered": len(series),
            "holders": latest["holders"],
            "holders_change_wow_pct": holders_wow,
            "holders_change_period_pct": holders_period,
            "avg_lots_per_holder": latest["avg_lots_per_holder"],
            "avg_lots_change_period_pct": avg_period,
            "big_400_pct": latest["big_400_pct"], "big_400_delta": big_delta,
            "whale_1000_people": latest["whale_1000_people"],
            "whale_1000_pct": latest["whale_1000_pct"], "whale_1000_delta": whale_delta,
            "retail_people_pct": latest["retail_people_pct"],
            "verdict": verdict, "what_this_means": meaning,
            "series": series,
            "level_distribution": level_dist,
            "limitations": [
                "集保股權分散表為週資料（以每週五結算），隔週才公布，你看到的通常是一週前狀態",
                "人數變化受除權息、增減資、可轉債轉換影響，數字跳動時要先確認有無公司行動",
                "籌碼集中不等於會漲；流通籌碼少的股票下殺時同樣沒人承接",
            ],
            "checked_at": datetime.now(TW_TZ).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


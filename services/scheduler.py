"""
TradeGuard 自動排程 v2
- 週一至五 09:00-13:40 每5分鐘：持倉 + 觀察清單 盤中警示 → LINE（30分鐘冷卻）
- 13:35 收盤最後掃描
- 14:30 收盤日報（含市場燈號、外資期貨OI、大盤維持率）→ LINE
- 21:05 夜間全市場快照（股價+法人+ADL 存 Mongo）
- 21:25 早期進場訊號掃描 →「明日進場候選」LINE
- 每月 11 日 08:40 月營收動能掃描 → LINE
"""
import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

_alert_cooldown: dict = {}
COOLDOWN_MINUTES = 30

def _on_cooldown(alert_id: str) -> bool:
    if alert_id not in _alert_cooldown:
        return False
    return (datetime.utcnow() - _alert_cooldown[alert_id]).total_seconds() / 60 < COOLDOWN_MINUTES

def _mark(alert_id: str):
    _alert_cooldown[alert_id] = datetime.utcnow()


async def _scan_one_stock(uid: str, ticker: str, name: str, market: str,
                          entry: float | None, highest: float | None,
                          pt: float, sl: float, is_holding: bool, msgs: list):
    from services.finmind import get_tw_technical
    from services.yfinance_service import calculate_technical_indicators, get_tw_realtime_price

    if market == "tw":
        tech = await get_tw_technical(ticker)
        rt = await get_tw_realtime_price(ticker)
    else:
        tech = await asyncio.to_thread(calculate_technical_indicators, ticker, market)
        rt = None
    if not tech:
        return

    fin_close = tech["current_price"]
    current = rt["current_price"] if rt and rt.get("current_price") else fin_close
    tag = "持倉" if is_holding else "觀察"
    ma = tech.get("ma", {})
    rsi = tech.get("rsi", 50)
    kd = tech.get("kd", {})

    if is_holding and entry:
        hi = max(current, highest or 0)
        pnl_pct = round((current - entry) / entry * 100, 2)
        pullback = round((hi - current) / hi * 100, 2) if hi > 0 else 0
        if pnl_pct > 0 and pullback >= pt:
            aid = f"{uid}_{ticker}_profit"
            if not _on_cooldown(aid):
                msgs.append(f"[停利] {ticker} {name} 回檔 {pullback:.1f}% 已觸發\n  現價 ${current}，損益 +{pnl_pct}%")
                _mark(aid)
        elif pnl_pct <= -sl:
            aid = f"{uid}_{ticker}_loss"
            if not _on_cooldown(aid):
                msgs.append(f"[停損] {ticker} {name} 虧損 {abs(pnl_pct):.1f}%\n  現價 ${current}")
                _mark(aid)

    for label, key in [("5日線", "ma5"), ("10日線", "ma10"), ("月線", "ma20"), ("季線", "ma60")]:
        mav = ma.get(key)
        if not mav:
            continue
        diff = round((current - mav) / mav * 100, 2)
        if abs(diff) <= 1.5:
            aid = f"{uid}_{ticker}_{key}"
            if not _on_cooldown(aid):
                direction = "站上" if diff >= 0 else "跌破"
                msgs.append(f"[{tag}·{label}] {ticker} {name} {direction}{label}（{diff:+.1f}%）\n  現價 ${current}，{label} ${mav}")
                _mark(aid)

    if kd.get("overbought") and rsi < 65:
        aid = f"{uid}_{ticker}_div_bear"
        if not _on_cooldown(aid):
            msgs.append(f"[{tag}·頂背離] {ticker} {name}\n  K{kd.get('k')} 超買但 RSI {rsi} 未跟上，留意回檔")
            _mark(aid)
    elif kd.get("oversold") and rsi > 35:
        aid = f"{uid}_{ticker}_div_bull"
        if not _on_cooldown(aid):
            msgs.append(f"[{tag}·底背離] {ticker} {name}\n  K{kd.get('k')} 超賣但 RSI {rsi} 回升，可能反彈")
            _mark(aid)

    # 觀察股「啟動」：盤中相對前收大漲
    if not is_holding and market == "tw" and rt and fin_close:
        day_chg = round((current - fin_close) / fin_close * 100, 2)
        if day_chg >= 4:
            aid = f"{uid}_{ticker}_launch"
            if not _on_cooldown(aid):
                vr = tech.get("volume", {}).get("ratio")
                msgs.append(f"[觀察·啟動] {ticker} {name} 盤中大漲 {day_chg:+.1f}%\n  現價 ${current}" + (f"，量比 {vr}x" if vr else ""))
                _mark(aid)


async def scan_all_users():
    from database import db
    from services.line_notify import send_line_message
    try:
        users = [u async for u in db.users.find({}, {"email": 1, "rules": 1})]
        for user in users:
            uid = user.get("email")
            if not uid:
                continue
            rules = user.get("rules", {})
            pt = rules.get("profit_trailing_pct", 20)
            sl = rules.get("stoploss_pct", 7)
            msgs: list = []
            async for h in db.holdings.find({"user_id": uid}):
                await _scan_one_stock(uid, h["ticker"], h.get("name", h["ticker"]),
                                      h.get("market", "tw"), h.get("entry_price"),
                                      h.get("highest_price", 0), pt, sl, True, msgs)
            async for w in db.watchlist.find({"user_id": uid, "market": "tw"}):
                await _scan_one_stock(uid, w["ticker"], w.get("name", w["ticker"]),
                                      "tw", None, None, pt, sl, False, msgs)
            if msgs:
                await send_line_message("TradeGuard 盤中警示\n" + "─" * 20 + "\n" + "\n\n".join(msgs))
    except Exception as e:
        print(f"[Scheduler] scan error: {e}")


async def daily_market_report():
    """14:30 收盤日報：燈號 + 指數 + 台灣市場 + 期貨籌碼"""
    from services.line_notify import send_line_message
    try:
        import yfinance as yf
        lines = [f"TradeGuard 收盤日報 {datetime.now().strftime('%Y-%m-%d')}", "─" * 25]

        try:
            from routers.market import compute_market_signal
            sig = await compute_market_signal()
            light_txt = {"green": "綠燈", "yellow": "黃燈", "red": "紅燈"}.get(sig["light"], "")
            lines.append(f"市場燈號 {sig['score']}/100 【{light_txt}】")
            lines.append(f"→ {sig['advice']}")
            worst = sorted(sig["components"], key=lambda x: x["score"])[:2]
            for w in worst:
                lines.append(f"  弱項 {w['name']}: {w['detail']}")
            foi = (sig.get("extras") or {}).get("futures_oi")
            if foi:
                lines.append(f"  外資期OI {foi['foreign_net_oi']:+,} 口（5日 {foi['chg5']:+,}）")
            mv = ((sig.get("extras") or {}).get("internals") or {}).get("maintenance", {})
            if mv.get("value"):
                lines.append(f"  大盤維持率 {mv['value']}%")
        except Exception as e:
            lines.append(f"燈號計算失敗: {e}")

        try:
            vix_hist = yf.Ticker("^VIX").history(period="2d")
            if not vix_hist.empty:
                v = round(float(vix_hist["Close"].iloc[-1]), 2)
                status = "【危險】" if v >= 30 else "【注意】" if v >= 20 else "【正常】"
                lines.append(f"\nVIX {status} {v}")
        except Exception:
            pass

        intl = {"S&P500": "^GSPC", "NASDAQ": "^IXIC", "費半SOX": "^SOX",
                "日經": "^N225", "KOSPI": "^KS11", "美元/台幣": "USDTWD=X"}
        lines.append("【國際指數】")
        for name, ticker in intl.items():
            try:
                hist = yf.Ticker(ticker).history(period="2d")
                if not hist.empty:
                    c = float(hist["Close"].iloc[-1])
                    p = float(hist["Close"].iloc[-2]) if len(hist) > 1 else c
                    chg = round((c - p) / p * 100, 2)
                    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "-"
                    lines.append(f"  {name}: {c:,.2f} {arrow}{abs(chg):.2f}%")
            except Exception:
                pass

        await send_line_message("\n".join(lines))
    except Exception as e:
        print(f"[Scheduler] daily report error: {e}")


async def nightly_snapshot_job():
    from services.snapshot import run_daily_snapshot, db_status
    try:
        out = await run_daily_snapshot()
        print(f"[Scheduler] snapshot: {out}")
        st = await db_status()
        if st["daily_days"] < 60 and not st["backfill"]["running"]:
            from services.snapshot import run_backfill
            asyncio.create_task(run_backfill(130))
            print("[Scheduler] 歷史不足，觸發自動回補")
    except Exception as e:
        print(f"[Scheduler] snapshot error: {e}")


async def early_signals_push():
    from services.line_notify import send_line_message
    try:
        from services.signals import scan_early
        r = await scan_early()
        if r.get("error"):
            print(f"[Scheduler] early signals: {r['error']}")
            return
        lines = [f"明日進場候選（{r.get('data_date','')} 收盤掃描）", "─" * 25]

        def block(title, items, fmt):
            if not items:
                return
            lines.append(f"\n【{title}】")
            for x in items[:5]:
                lines.append("  " + fmt(x))

        block("爆量未大漲（主力吸籌）", r.get("vol_quiet", []),
              lambda x: f"{x['ticker']} {x['name']} 量比{x['vol_ratio']}x 漲{x['chg_pct']}% ${x['close']}")
        block("均線糾結突破（起漲）", r.get("squeeze", []),
              lambda x: f"{x['ticker']} {x['name']} 糾結{x['squeeze_pct']}% 突破+{x['chg_pct']}% 量{x['vol_ratio']}x")
        block("60日新高（趨勢確認）", r.get("high60", []),
              lambda x: f"{x['ticker']} {x['name']} +{x['chg_pct']}% 量{x['vol_ratio']}x ${x['close']}")
        block("投信連買未噴", r.get("trust", []),
              lambda x: f"{x['ticker']} {x['name']} 連買{x['streak']}日 共{x['total_lots']:.0f}張 5日{x['chg5_pct']:+.1f}%")
        block("族群落後補漲", r.get("laggards", []),
              lambda x: f"{x['sector']}: 龍頭{x['leader_name']}+{x['leader_ret20']}% → {x['ticker']} {x['name']} 僅+{x['ret20']}%")

        if len(lines) <= 2:
            lines.append("今日無符合條件的早期訊號")
        lines.append("\n※ 候選非建議，請照自己的進場規則確認量價與大盤燈號")
        await send_line_message("\n".join(lines))
    except Exception as e:
        print(f"[Scheduler] early signals error: {e}")


async def monthly_revenue_job():
    from services.line_notify import send_line_message
    from database import db
    try:
        from services.signals import revenue_momentum
        tickers = set()
        try:
            from routers.scan import TW_THEME_SECTORS
            for lst in TW_THEME_SECTORS.values():
                tickers.update(lst)
        except Exception:
            pass
        async for h in db.holdings.find({"market": "tw"}, {"ticker": 1}):
            tickers.add(h["ticker"])
        async for w in db.watchlist.find({"market": "tw"}, {"ticker": 1}):
            tickers.add(w["ticker"])
        items = await revenue_momentum(sorted(tickers))
        lines = [f"月營收動能掃描 {datetime.now().strftime('%Y-%m')}", "─" * 25]
        for x in items[:12]:
            flag = "創高" if x["record_high"] else ""
            lines.append(f"  {x['ticker']} {x['name']} YoY {x['yoy'][-1]}%（前兩月 {x['yoy'][0]}→{x['yoy'][1]}）{flag}")
        if len(lines) <= 2:
            lines.append("本月無符合加速條件標的")
        await send_line_message("\n".join(lines))
    except Exception as e:
        print(f"[Scheduler] revenue error: {e}")


def start_scheduler():
    scheduler.add_job(scan_all_users,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/5", timezone="Asia/Taipei"),
        id="intraday_scan", replace_existing=True)
    scheduler.add_job(scan_all_users,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=35, timezone="Asia/Taipei"),
        id="closing_scan", replace_existing=True)
    scheduler.add_job(daily_market_report,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=30, timezone="Asia/Taipei"),
        id="daily_report", replace_existing=True)
    scheduler.add_job(nightly_snapshot_job,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=5, timezone="Asia/Taipei"),
        id="nightly_snapshot", replace_existing=True)
    scheduler.add_job(early_signals_push,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=25, timezone="Asia/Taipei"),
        id="early_signals", replace_existing=True)
    scheduler.add_job(monthly_revenue_job,
        CronTrigger(day=11, hour=8, minute=40, timezone="Asia/Taipei"),
        id="monthly_revenue", replace_existing=True)
    scheduler.start()
    print("[Scheduler] v2 啟動：盤中5分鐘 + 14:30日報 + 21:05快照 + 21:25候選 + 每月11日營收")

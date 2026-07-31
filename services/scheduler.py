"""
TradeGuard 自動排程
- 週一至週五 09:00-13:40 每 5 分鐘掃描持倉警示 + 均線觸碰 + 背離 → LINE
- 週一至週五 14:30 完整大盤日報 → LINE
- 重複警示 30 分鐘內不重送（避免洗版）
"""
import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

# 冷卻記錄：避免同一警示在 30 分鐘內重複傳送
_alert_cooldown: dict[str, datetime] = {}
COOLDOWN_MINUTES = 30

def _on_cooldown(alert_id: str) -> bool:
    if alert_id not in _alert_cooldown:
        return False
    elapsed = (datetime.utcnow() - _alert_cooldown[alert_id]).total_seconds() / 60
    return elapsed < COOLDOWN_MINUTES

def _mark(alert_id: str):
    _alert_cooldown[alert_id] = datetime.utcnow()


async def scan_all_users():
    """每 5 分鐘：掃描所有用戶持倉的停利停損 + 均線觸碰 + 背離"""
    from database import db
    from services.line_notify import send_line_message
    from services.finmind import get_tw_technical
    from services.yfinance_service import calculate_technical_indicators

    try:
        users = [u async for u in db.users.find({}, {"email": 1, "rules": 1})]
        for user in users:
            uid = user.get("email")
            if not uid:
                continue
            rules = user.get("rules", {})
            pt = rules.get("profit_trailing_pct", 20)
            sl = rules.get("stoploss_pct", 7)
            msgs = []

            async for h in db.holdings.find({"user_id": uid}):
                ticker = h["ticker"]
                market = h.get("market", "tw")
                name   = h.get("name", ticker)

                if market == "tw":
                    tech = await get_tw_technical(ticker)
                else:
                    tech = await asyncio.to_thread(calculate_technical_indicators, ticker, market)
                if not tech:
                    continue

                current  = tech["current_price"]
                entry    = h["entry_price"]
                highest  = max(current, h.get("highest_price", 0))
                pnl_pct  = round((current - entry) / entry * 100, 2)
                pullback = round((highest - current) / highest * 100, 2) if highest > 0 else 0
                ma       = tech.get("ma", {})
                rsi      = tech.get("rsi", 50)
                kd       = tech.get("kd", {})

                # ── 停利停損 ──
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

                # ── 均線觸碰（±1.5% 以內）──
                for label, key in [("5日線", "ma5"), ("10日線", "ma10"), ("月線", "ma20"), ("季線", "ma60")]:
                    mav = ma.get(key)
                    if not mav:
                        continue
                    diff = round((current - mav) / mav * 100, 2)
                    if abs(diff) <= 1.5:
                        aid = f"{uid}_{ticker}_{key}"
                        if not _on_cooldown(aid):
                            direction = "站上" if diff >= 0 else "跌破"
                            msgs.append(f"[{label}] {ticker} {name} {direction} {label}（{diff:+.1f}%）\n  現價 ${current}，{label} ${mav}")
                            _mark(aid)

                # ── 背離 ──
                if kd.get("overbought") and rsi < 65:
                    aid = f"{uid}_{ticker}_div_bear"
                    if not _on_cooldown(aid):
                        msgs.append(f"[頂背離] {ticker} {name}\n  KD超買但RSI={rsi}動能不足，注意風險")
                        _mark(aid)
                elif kd.get("oversold") and rsi > 35:
                    aid = f"{uid}_{ticker}_div_bull"
                    if not _on_cooldown(aid):
                        msgs.append(f"[底背離] {ticker} {name}\n  KD超賣但RSI={rsi}回升，可能反彈")
                        _mark(aid)

            if msgs:
                text = "TradeGuard 盤中警示\n" + "─" * 20 + "\n" + "\n\n".join(msgs)
                await send_line_message(text)

    except Exception as e:
        print(f"[Scheduler] scan error: {e}")


async def daily_market_report():
    """14:30 完整大盤收盤日報"""
    from services.line_notify import send_line_message
    try:
        import yfinance as yf
        lines = [
            f"TradeGuard 收盤日報 {datetime.now().strftime('%Y-%m-%d')}",
            "─" * 25,
        ]

        # VIX
        try:
            vix_hist = yf.Ticker("^VIX").history(period="2d")
            if not vix_hist.empty:
                v = round(float(vix_hist["Close"].iloc[-1]), 2)
                status = "【危險】" if v >= 30 else "【注意】" if v >= 20 else "【正常】"
                lines.append(f"VIX {status} {v}")
        except:
            pass

        # 國際指數
        intl = {
            "S&P500": "^GSPC", "NASDAQ": "^IXIC",
            "費半SOX": "^SOX", "日經": "^N225", "KOSPI": "^KS11",
            "美元指數": "DX-Y.NYB", "美債10Y": "^TNX", "美元/台幣": "USDTWD=X",
        }
        lines.append("\n【國際指數】")
        for name, ticker in intl.items():
            try:
                hist = yf.Ticker(ticker).history(period="2d")
                if not hist.empty:
                    c = float(hist["Close"].iloc[-1])
                    p = float(hist["Close"].iloc[-2]) if len(hist) > 1 else c
                    chg = round((c - p) / p * 100, 2)
                    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "-"
                    lines.append(f"  {name}: {c:,.2f} {arrow}{abs(chg):.2f}%")
            except:
                pass

        # 台灣市場
        from services.finmind import get_tw_index_data, get_tw_futures_data, get_tw_margin_balance, get_tw_institutional
        from services.twse import fetch_twse_stock_performance
        lines.append("\n【台灣市場】")

        tw = await get_tw_index_data()
        if tw:
            arrow = "▲" if tw["change_pct"] > 0 else "▼"
            lines.append(f"  加權指數: {tw['current']:,.2f} {arrow}{abs(tw['change_pct']):.2f}%  RSI:{tw['rsi']}  KD:K{tw['kd']['k']}/D{tw['kd']['d']}")

        fut = await get_tw_futures_data()
        if fut:
            arrow = "▲" if fut["change_pct"] > 0 else "▼"
            lines.append(f"  台指期TX: {fut['current']:,.0f} {arrow}{abs(fut['change_pct']):.2f}%")

        margin = await get_tw_margin_balance()
        if margin:
            lines.append(f"  融資餘額: {margin['balance']}億 ({margin['change_pct']:+.2f}% {margin['trend']})")

        inst = await get_tw_institutional()
        if inst:
            lines.append(f"  外資近5日: {inst['foreign_net_5d']}億 ({inst['trend']})")

        perf = await fetch_twse_stock_performance()
        if perf:
            up   = sum(1 for v in perf.values() if v["change_pct"] > 0)
            down = sum(1 for v in perf.values() if v["change_pct"] < 0)
            limit_up   = sum(1 for v in perf.values() if v.get("to_limit_pct", 999) <= 0.1)
            limit_down = sum(1 for v in perf.values() if v["change_pct"] <= -9.5)
            ratio = round(up / down, 2) if down > 0 else 99
            breadth = "強勢" if ratio > 2 else "弱勢" if ratio < 0.5 else "平衡"
            lines.append(f"  漲跌家數: {up}↑ {down}↓  漲停{limit_up}/跌停{limit_down} [{breadth}]")

        await send_line_message("\n".join(lines))
    except Exception as e:
        print(f"[Scheduler] daily report error: {e}")


def start_scheduler():
    """在 app lifespan 中呼叫，啟動所有排程"""
    # 盤中掃描：週一至週五 09:00-13:40，每 5 分鐘
    scheduler.add_job(
        scan_all_users,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/5", timezone="Asia/Taipei"),
        id="intraday_scan", replace_existing=True,
    )
    # 13:35 收盤最後掃描
    scheduler.add_job(
        scan_all_users,
        CronTrigger(day_of_week="mon-fri", hour=13, minute=35, timezone="Asia/Taipei"),
        id="closing_scan", replace_existing=True,
    )
    # 14:30 完整日報
    scheduler.add_job(
        daily_market_report,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=30, timezone="Asia/Taipei"),
        id="daily_report", replace_existing=True,
    )
    scheduler.start()
    print("[Scheduler] 啟動：盤中每5分鐘 + 14:30日報")

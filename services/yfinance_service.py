import yfinance as yf
import pandas as pd
import httpx
from datetime import datetime, timezone, timedelta

def get_yf_ticker(ticker: str, market: str) -> str:
    if market != "tw":
        return ticker
    return f"{ticker}.TW"

def get_yf_ticker_with_fallback(ticker: str) -> list:
    """回傳台股可能的 yfinance ticker 清單（先試上市.TW，再試上櫃.TWO）"""
    return [f"{ticker}.TW", f"{ticker}.TWO"]

def is_tw_trading_hours() -> bool:
    """判斷現在是否為台股交易時間（台灣時間 09:00-13:35，週一至週五）"""
    tw_tz = timezone(timedelta(hours=8))
    now = datetime.now(tw_tz)
    if now.weekday() >= 5:  # 週六日
        return False
    t = now.hour * 100 + now.minute
    return 900 <= t <= 1335

async def get_tw_realtime_price(ticker: str) -> dict | None:
    """盤中用 TWSE MIS API 取得即時價格"""
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{ticker}.tw&json=1&delay=0"
        async with httpx.AsyncClient(timeout=5, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        msg = data.get("msgArray", [])
        if not msg:
            # 試 OTC（上櫃）
            url2 = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=otc_{ticker}.tw&json=1&delay=0"
            async with httpx.AsyncClient(timeout=5, verify=False) as client:
                r2 = await client.get(url2, headers={"User-Agent": "Mozilla/5.0"})
            data2 = r2.json()
            msg = data2.get("msgArray", [])
        if msg:
            item = msg[0]
            price = float(item.get("z", item.get("y", 0)) or 0)
            if price > 0:
                return {"current_price": price, "source": "realtime"}
    except:
        pass
    return None

def calculate_ma(prices: pd.Series, period: int):
    if len(prices) < period:
        return None
    return round(prices.tail(period).mean(), 2)

def get_stock_data(ticker: str, market: str) -> dict:
    tickers_to_try = get_yf_ticker_with_fallback(ticker) if market == "tw" else [ticker]
    for yf_ticker in tickers_to_try:
        try:
            stock = yf.Ticker(yf_ticker)
            hist = stock.history(period="6mo")
            if hist.empty:
                continue
            closes = hist["Close"]
            current_price = round(float(closes.iloc[-1]), 2)
            highest_price = round(float(closes.max()), 2)
            ma20 = calculate_ma(closes, 20)
            ma60 = calculate_ma(closes, 60)
            ma20_diff = round((current_price - ma20) / ma20 * 100, 2) if ma20 else None
            ma60_diff = round((current_price - ma60) / ma60 * 100, 2) if ma60 else None
            return {
                "current_price": current_price,
                "highest_price": highest_price,
                "ma20": ma20, "ma60": ma60,
                "ma20_diff_pct": ma20_diff,
                "ma60_diff_pct": ma60_diff,
            }
        except Exception as e:
            print(f"Error fetching {yf_ticker}: {e}")
            continue
    return None

def calculate_technical_indicators(ticker: str, market: str) -> dict:
    yf_ticker = get_yf_ticker(ticker, market)
    try:
        stock = yf.Ticker(yf_ticker)
        hist = stock.history(period="1y")
        if len(hist) < 60:
            return None

        closes = hist["Close"]
        highs = hist["High"]
        lows = hist["Low"]
        volumes = hist["Volume"]
        current = float(closes.iloc[-1])

        ma5   = round(float(closes.tail(5).mean()), 2)
        ma20  = round(float(closes.tail(20).mean()), 2)
        ma60  = round(float(closes.tail(60).mean()), 2)
        ma120 = round(float(closes.tail(120).mean()), 2) if len(closes) >= 120 else None
        ma240 = round(float(closes.tail(240).mean()), 2) if len(closes) >= 240 else None
        ma_bullish = current > ma20 > ma60
        ma_bearish = current < ma20 < ma60

        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = round(float(100 - (100 / (1 + rs.iloc[-1]))), 1)

        low9  = lows.rolling(9).min()
        high9 = highs.rolling(9).max()
        rsv   = (closes - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2).mean()
        d = k.ewm(com=2).mean()
        k_val = round(float(k.iloc[-1]), 1)
        d_val = round(float(d.iloc[-1]), 1)
        k_prev = round(float(k.iloc[-2]), 1)
        d_prev = round(float(d.iloc[-2]), 1)
        kd_golden = k_prev < d_prev and k_val > d_val
        kd_death  = k_prev > d_prev and k_val < d_val
        kd_overbought = k_val > 80
        kd_oversold   = k_val < 20

        ema12 = closes.ewm(span=12).mean()
        ema26 = closes.ewm(span=26).mean()
        macd_line   = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        histogram   = macd_line - signal_line
        macd_val   = round(float(macd_line.iloc[-1]), 3)
        signal_val = round(float(signal_line.iloc[-1]), 3)
        hist_val   = round(float(histogram.iloc[-1]), 3)
        hist_prev  = round(float(histogram.iloc[-2]), 3)
        macd_bullish  = macd_val > signal_val
        macd_expanding = hist_val > hist_prev > 0
        macd_shrinking = hist_val < hist_prev < 0

        bb_mid   = closes.tail(20).mean()
        bb_std   = closes.tail(20).std()
        bb_upper = round(float(bb_mid + 2 * bb_std), 2)
        bb_lower = round(float(bb_mid - 2 * bb_std), 2)
        bb_mid   = round(float(bb_mid), 2)
        bb_pct   = round((current - bb_lower) / (bb_upper - bb_lower) * 100, 1)

        vol_today = float(volumes.iloc[-1])
        vol_ma20  = float(volumes.tail(20).mean())
        vol_ratio = round(vol_today / vol_ma20, 2) if vol_ma20 > 0 else 1.0
        vol_surge = vol_ratio > 1.5

        bull_signals, bear_signals = [], []
        if kd_golden:    bull_signals.append("KD 黃金交叉")
        if kd_death:     bear_signals.append("KD 死亡交叉")
        if kd_oversold:  bull_signals.append(f"KD 超賣（K={k_val}）")
        if kd_overbought: bear_signals.append(f"KD 超買（K={k_val}）")
        if rsi < 30:     bull_signals.append(f"RSI 超賣（{rsi}）")
        elif rsi > 70:   bear_signals.append(f"RSI 超買（{rsi}）")
        elif 45 < rsi < 65: bull_signals.append(f"RSI 健康偏多（{rsi}）")
        if macd_bullish:   bull_signals.append("MACD 在信號線上方")
        else:              bear_signals.append("MACD 在信號線下方")
        if macd_expanding: bull_signals.append("MACD 柱狀體擴張")
        if macd_shrinking: bear_signals.append("MACD 柱狀體收縮")
        if ma_bullish:  bull_signals.append("均線多頭排列")
        if ma_bearish:  bear_signals.append("均線空頭排列")
        if current > ma20: bull_signals.append("站上月線")
        else:              bear_signals.append("跌破月線")
        if current > ma60: bull_signals.append("站上季線")
        else:              bear_signals.append("跌破季線")
        if vol_surge and current > closes.iloc[-2]: bull_signals.append(f"量增價漲（量比 {vol_ratio}x）")
        elif vol_surge and current < closes.iloc[-2]: bear_signals.append(f"量增價跌（量比 {vol_ratio}x）")
        if bb_pct > 80:  bear_signals.append(f"接近布林上緣（{bb_pct}%）")
        elif bb_pct < 20: bull_signals.append(f"接近布林下緣（{bb_pct}%）")

        bull_count = len(bull_signals)
        bear_count = len(bear_signals)
        if bull_count >= bear_count + 2:   direction = "偏多"
        elif bear_count >= bull_count + 2: direction = "偏空"
        else:                              direction = "中性"

        return {
            "ticker": ticker, "market": market,
            "current_price": round(current, 2),
            "ma": {"ma5": ma5, "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma240": ma240},
            "ma20_diff_pct": round((current - ma20) / ma20 * 100, 2),
            "ma60_diff_pct": round((current - ma60) / ma60 * 100, 2),
            "rsi": rsi,
            "kd": {"k": k_val, "d": d_val, "golden_cross": kd_golden, "death_cross": kd_death,
                   "overbought": kd_overbought, "oversold": kd_oversold},
            "macd": {"macd": macd_val, "signal": signal_val, "histogram": hist_val,
                     "bullish": macd_bullish, "expanding": macd_expanding},
            "bollinger": {"upper": bb_upper, "mid": bb_mid, "lower": bb_lower, "pct": bb_pct},
            "volume": {"ratio": vol_ratio, "surge": vol_surge},
            "bull_signals": bull_signals, "bear_signals": bear_signals, "direction": direction,
        }
    except Exception as e:
        print(f"Technical analysis error {ticker}: {e}")
        return None

def calculate_status(current_price, entry_price, highest_price, rules) -> str:
    pnl_pct = (current_price - entry_price) / entry_price * 100
    trailing_pct = (highest_price - current_price) / highest_price * 100 if highest_price > 0 else 0
    profit_trigger  = rules.get("profit_trailing_pct", 20)
    stoploss_trigger = rules.get("stoploss_pct", 7)
    if pnl_pct >= 20 and trailing_pct >= profit_trigger:   return "profit_alert"
    elif pnl_pct <= -stoploss_trigger:                     return "loss_alert"
    elif trailing_pct >= profit_trigger * 0.8:             return "watch"
    elif pnl_pct <= -stoploss_trigger * 0.7:               return "watch"
    else:                                                  return "ok"

def scan_sector_yf(tickers: list, market: str) -> dict:
    results = []
    for ticker in tickers:
        try:
            stock = yf.Ticker(get_yf_ticker(ticker, market))
            hist = stock.history(period="10d")
            if len(hist) < 2:
                continue
            today_close = float(hist["Close"].iloc[-1])
            prev_close  = float(hist["Close"].iloc[-2])
            change_pct  = round((today_close - prev_close) / prev_close * 100, 2)
            five_days_ago = float(hist["Close"].iloc[-6]) if len(hist) >= 6 else float(hist["Close"].iloc[0])
            change_5d   = round((today_close - five_days_ago) / five_days_ago * 100, 2)
            today_vol   = float(hist["Volume"].iloc[-1])
            avg_vol     = float(hist["Volume"].tail(5).mean())
            vol_ratio   = round(today_vol / avg_vol, 1) if avg_vol > 0 else 1.0
            results.append({
                "ticker": ticker, "name": ticker,
                "current_price": round(today_close, 2),
                "change_pct": change_pct, "change_5d_pct": change_5d,
                "vol_ratio": vol_ratio, "to_limit_pct": None,
                "is_hot": change_5d >= 10,
            })
        except Exception as e:
            print(f"Scan error {ticker}: {e}")
    if not results:
        return {"avg_change_pct": 0, "avg_vol_ratio": 1.0, "stocks": []}
    avg_change    = round(sum(r["change_pct"] for r in results) / len(results), 2)
    avg_vol_ratio = round(sum(r["vol_ratio"] for r in results) / len(results), 1)
    return {
        "avg_change_pct": avg_change, "avg_vol_ratio": avg_vol_ratio,
        "stocks": sorted(results, key=lambda x: x["change_pct"], reverse=True)
    }
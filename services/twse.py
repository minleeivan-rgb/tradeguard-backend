import io
import httpx
import pandas as pd
from datetime import datetime

_twse_cache = {"date": None, "industry_map": {}, "stock_names": {}}
_perf_cache  = {"date": None, "data": {}}

async def fetch_twse_industry_map():
    global _twse_cache
    today = datetime.now().strftime("%Y%m%d")
    if _twse_cache["date"] == today and _twse_cache["industry_map"]:
        return _twse_cache["industry_map"], _twse_cache["stock_names"]

    urls = [
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2",
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4",
        "https://isin.twse.com.tw/isin/C_public.jsp?strMode=5",
    ]
    industry_map, stock_names = {}, {}

    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            content = r.content.decode("big5", errors="replace")
            tables = pd.read_html(io.StringIO(content))
            df = tables[0]
            for _, row in df.iterrows():
                try:
                    cell = str(row.iloc[0])
                    parts = cell.split("\u3000") if "\u3000" in cell else cell.split("　")
                    if len(parts) < 2:
                        continue
                    code = parts[0].strip()
                    name = parts[1].strip()
                    if not (code.isdigit() and 4 <= len(code) <= 5):
                        continue
                    industry = str(row.iloc[4]).strip() if len(row) > 4 else ""
                    if not industry or industry == "nan":
                        continue
                    stock_names[code] = name
                    industry_map.setdefault(industry, []).append(code)
                except:
                    continue
        except Exception as e:
            print(f"[TWSE] {url} 失敗：{e}")
            continue

    if industry_map:
        _twse_cache.update({"date": today, "industry_map": industry_map, "stock_names": stock_names})
        print(f"[TWSE] 載入 {len(stock_names)} 支股票，{len(industry_map)} 個產業（含上櫃興櫃）")
    return industry_map, stock_names


async def fetch_twse_stock_performance():
    global _perf_cache
    today = datetime.now().strftime("%Y%m%d")

    # FIX: 同日快取，避免重複 fetch
    if _perf_cache["date"] == today and _perf_cache["data"]:
        return _perf_cache["data"]

    try:
        url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json"
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        performance = {}
        if data.get("stat") == "OK":
            fields = data.get("fields", [])
            close_idx  = next((i for i, f in enumerate(fields) if "收盤" in str(f)), None)
            change_idx = next((i for i, f in enumerate(fields) if "漲跌價差" in str(f)), None)
            vol_idx    = next((i for i, f in enumerate(fields) if "成交股數" in str(f)), None)
            for item in data.get("data", []):
                try:
                    code = str(item[0]).strip()
                    name = str(item[1]).strip()
                    if not (code.isdigit() and 4 <= len(code) <= 5):
                        continue
                    close      = float(str(item[close_idx]).replace(",", "")) if close_idx is not None else 0
                    change_amt = float(str(item[change_idx]).replace(",", "").replace("--", "0")) if change_idx is not None else 0
                    vol        = float(str(item[vol_idx]).replace(",", "")) if vol_idx is not None else 0
                    prev_close = close - change_amt
                    change_pct = round(change_amt / prev_close * 100, 2) if prev_close > 0 else 0
                    limit_price  = round(prev_close * 1.10, 2)
                    to_limit_pct = round((limit_price - close) / close * 100, 2) if close > 0 else None
                    performance[code] = {
                        "name": name, "current_price": close,
                        "change_pct": change_pct, "change_amt": change_amt,
                        "volume": vol, "to_limit_pct": to_limit_pct,
                    }
                except:
                    continue
        print(f"[TWSE] 取得 {len(performance)} 支股票今日資料")
        if performance:
            _perf_cache.update({"date": today, "data": performance})
        return performance
    except Exception as e:
        print(f"[TWSE] STOCK_DAY_ALL 載入失敗：{e}")
        return {}

import asyncio
from fastapi import APIRouter
from datetime import datetime
from services.twse import fetch_twse_industry_map, fetch_twse_stock_performance
from services.yfinance_service import scan_sector_yf

router = APIRouter(prefix="/scan", tags=["scan"])

# ── 台股完整名稱對照 ──
TW_STOCK_LIST = {
    "2330":"台積電","2303":"聯電","5347":"世界先進","6770":"力積電",
    "2454":"聯發科","3034":"聯詠","2379":"瑞昱","6415":"矽力-KY","3661":"世芯-KY",
    "5269":"祥碩","5274":"信驊","6533":"晶心科","4919":"新唐","8081":"致新",
    "3598":"奕力","4966":"譜瑞-KY","2458":"義隆","3692":"其陽","3707":"漢磊",
    "2408":"南亞科","2337":"旺宏","2344":"華邦電","8299":"群聯","4967":"十銓","6239":"力旺",
    "3711":"日月光投控","2449":"京元電子","6257":"矽格","8150":"南茂",
    "6147":"頎邦","3264":"欣銓","6223":"旺矽","6510":"精測","6515":"穎崴",
    "2441":"超豐","6271":"同欣電","8131":"福懋科","2369":"菱生","3265":"台星科",
    "2329":"華泰","6261":"久元","3374":"精材","3372":"典範","3450":"聯鈞","6552":"易華電",
    "2382":"廣達","2317":"鴻海","3231":"緯創","2356":"英業達","6669":"緯穎",
    "2301":"光寶科","2324":"仁寶","3706":"神達","2352":"佳世達",
    "3017":"奇鋐","4763":"材料-KY","8097":"常珵","6230":"超眾","3324":"雙鴻",
    "2421":"建準","3338":"泰碩","3233":"丹陽",
    "3189":"景碩","8046":"南電","6269":"台郡","2383":"台光電","3037":"欣興",
    "6274":"台燿","3044":"健鼎","2368":"金像電","2370":"耀華","8155":"博智","2313":"華通","6284":"佳邦",
    "2327":"國巨","2492":"華新科","3026":"禾伸堂","2428":"興勤","2456":"奇力新",
    "6155":"鈞寶","3236":"千如","6224":"聚鼎","6173":"信昌電","2478":"大毅",
    "3624":"光頡","4760":"勤凱","5228":"鈺鎧","6432":"今展科","6127":"九豪",
    "2308":"台達電","6282":"康舒","2385":"群光","6138":"茂達","3317":"尼克森",
    "3617":"碩天","2457":"飛宏","3035":"智原",
    "2345":"智邦","3596":"合勤","3380":"明泰","5388":"中磊","2332":"友訊",
    "4906":"正文","6285":"啟碁","3491":"昇達科",
    "3008":"大立光","3406":"玉晶光","3362":"先進光","3019":"亞光","4974":"亞泰",
    "2409":"友達","3481":"群創","6116":"彩晶","3592":"瑞鼎",
    "2404":"漢唐","3583":"辛耘","3131":"弘塑","6436":"天虹","5443":"均豪","6196":"帆宣",
    "2392":"正崴","5457":"宣德","3533":"嘉澤","6290":"良維","6205":"詮欣","5227":"立昂","3023":"信邦",
    "2207":"和泰車","2227":"裕日車","3665":"貿聯-KY","4551":"智伸科","3205":"佰鈺","2355":"敬鵬",
    "4152":"台微體","4174":"浩鼎","6499":"益安","1760":"寶齡富錦",
    "4130":"健亞","4157":"太景-KY","4205":"中天","6548":"長聖",
    "6443":"元晶","6244":"茂迪","3514":"昱晶","6477":"安集","3691":"碩禾","3576":"新日光","6803":"崑鼎",
    "2881":"富邦金","2882":"國泰金","2891":"中信金","2886":"兆豐金",
    "2884":"玉山金","2887":"台新金","2892":"第一金","5880":"合庫金",
    "2883":"開發金","2885":"元大金","2880":"華南金","2890":"永豐金",
    "2603":"長榮","2609":"陽明","2615":"萬海","2637":"慧洋-KY","2636":"台驊投控","2612":"中航","2605":"新興",
    "2002":"中鋼","2015":"豐興","2023":"燁輝","2006":"東和鋼鐵","2029":"盛餘","2022":"聚亨","2010":"春源",
    "1301":"台塑","1303":"南亞","1326":"台化","6505":"台塑化","1213":"大連","1309":"台達化","1310":"台苯",
    "1216":"統一","1201":"味全","1215":"卜蜂","1210":"大成","1218":"泰山","1217":"愛之味","1229":"聯華","2723":"美食-KY",
    "2412":"中華電","3045":"台灣大","4904":"遠傳",
    "2610":"中華航","2618":"長榮航","2731":"雄獅","5706":"鳳凰","2707":"晶華","2701":"萬企","2705":"六福",
    "3083":"網龍","3293":"鈊象","5478":"智冠","3546":"宇峻","6720":"凱鈿",
    "1101":"台泥","1102":"亞泥","9907":"統一實","2542":"興富發","5522":"遠雄","2505":"國揚",
    "2049":"上銀","1597":"直得","2395":"研華","2359":"所羅門","1560":"新代","1536":"和大","1537":"廣隆",
    "2201":"裕隆","2204":"中華車","2206":"三陽工業","6605":"帝寶",
    "4938":"和碩",
    "3836":"萊德光電","2314":"台揚","3163":"波若威","3030":"熒茂","6573":"虹冠電",
}

# ── 35 個主題族群 ──
TW_THEME_SECTORS = {
    "晶圓代工":         ["2330","2303","5347","6770"],
    "IC設計":           ["2454","3034","2379","6415","3661","5269","5274","6533","4919","8081","3598","4966","2458","3692"],
    "記憶體/儲存":      ["2408","2337","2344","8299","4967","6239"],
    "IC封測":           ["3711","2449","6257","8150","6147","3264","6223","6510","6515","2441","6271","8131","2369","3265","2329","6261","3374","3372","3450","6552"],
    "AI伺服器/ODM":     ["2382","2317","3231","2356","6669","2301","2324","3706","2352"],
    "散熱/液冷":        ["3017","4763","8097","6230","3324","2421","3338","3233"],
    "PCB/載板":         ["3189","8046","6269","2383","3037","6274","3044","2368","2370","8155","2313","6284"],
    "被動元件":         ["2327","2492","3026","2428","2456","6155","3236","6224","6173","2478","3624","4760","5228","6432","6127"],
    "電源管理/UPS":     ["2308","6282","2385","6138","3317","3617","2457","3035"],
    "網通/交換器":      ["2345","3596","3380","5388","2332","4906","6285","3491"],
    "光學/鏡頭":        ["3008","3406","3362","3019","4974"],
    "面板/顯示":        ["2409","3481","6116","3592"],
    "半導體設備/材料":  ["2404","3583","3131","6436","5443","6196"],
    "連接器/線材":      ["2392","5457","3533","6290","6205","5227","3023"],
    "電動車/車用電子":  ["2207","2227","3665","4551","3205","2355"],
    "生技醫療":         ["4152","4174","6499","1760","4130","4157","4205","6548"],
    "太陽能/綠能/儲能": ["6443","6244","3514","6477","3691","3576","6803"],
    "金融保險":         ["2881","2882","2891","2886","2884","2887","2892","5880","2883","2885","2880","2890"],
    "航運":             ["2603","2609","2615","2637","2636","2612","2605"],
    "鋼鐵/金屬":        ["2002","2015","2023","2006","2029","2022","2010"],
    "石化/塑膠":        ["1301","1303","1326","6505","1213","1309","1310"],
    "食品飲料":         ["1216","1201","1215","1210","1218","1217","1229","2723"],
    "電信":             ["2412","3045","4904"],
    "航空/旅遊觀光":    ["2610","2618","2731","5706","2707","2701","2705"],
    "遊戲/軟體/SaaS":   ["3083","3293","5478","3546","6720"],
    "水泥/建材/營造":   ["1101","1102","9907","2542","5522","2505"],
    "機械/自動化":      ["2049","1597","2395","2359","1560","1536","1537"],
    "汽車/零件":        ["2201","2204","2206","6605","1536"],
    "電子代工/EMS":     ["2317","4938","2382","2324","2356","3231"],
    "低軌衛星":         ["2313","2383","3491","6285","3836","2314","6271","2458","6284","3163","6573"],
}

OTHER_MAP = {
    "其他電子":  ["光電業","資訊服務業","其他電子業","數位雲端"],
    "其他工業":  ["橡膠工業","玻璃陶瓷","造紙工業","紡織纖維","電機機械","電器電纜"],
    "其他金融":  ["金融保險"],
    "其他消費":  ["貿易百貨","觀光餐旅","運動休閒","居家生活"],
    "其他/雜項": ["其他","綜合","綠能環保","化學工業","建材營造","汽車工業","油電燃氣業","水泥工業","食品工業","塑膠工業","鋼鐵工業"],
}

US_SECTORS = {
    "AI/半導體":  ["NVDA","AMD","AVGO","QCOM","INTC","MU","TSM"],
    "雲端/SaaS":  ["MSFT","AMZN","GOOGL","CRM","SNOW","DDOG"],
    "社群/廣告":  ["META","SNAP","PINS","RDDT"],
    "科技硬體":   ["AAPL","DELL","HPE","SMCI","ANET"],
    "電動車/能源":["TSLA","RIVN","NIO","ENPH","FSLR"],
    "金融科技":   ["PYPL","SQ","COIN","HOOD","NU"],
}

def _calc_score(sector_name, stocks_data, total_vol):
    n = len(stocks_data)
    total_vol = total_vol or 1
    hot_count  = sum(1 for s in stocks_data if s["change_pct"] >= 5)
    hot_ratio  = hot_count / n
    vol_w      = sum(s["change_pct"] * (s["volume"] / total_vol) for s in stocks_data)
    hot_vol    = sum(s["volume"] for s in stocks_data if s["change_pct"] >= 3)
    vol_conc   = hot_vol / total_vol
    score      = (hot_ratio * 0.6) + (min(vol_w / 10, 1) * 0.2) + (vol_conc * 0.2)
    top5       = sorted(stocks_data, key=lambda x: x["change_pct"], reverse=True)[:5]
    display    = round(sum(s["change_pct"] for s in top5) / len(top5), 2)
    return {
        "sector": sector_name, "avg_change_pct": display,
        "strength_score": round(score, 4), "hot_count": hot_count,
        "stock_count": n, "avg_vol_ratio": round(vol_conc, 2),
        "stocks": sorted(stocks_data, key=lambda x: x["change_pct"], reverse=True)[:8]
    }

@router.get("/tw")
async def scan_tw():
    industry_map, stock_names = await fetch_twse_industry_map()
    performance = await fetch_twse_stock_performance()
    if not performance:
        return {"date": datetime.now().strftime("%Y-%m-%d"), "market": "tw",
                "top_sectors": [], "error": "無法取得證交所資料", "scanned_at": datetime.utcnow().isoformat()}

    results, themed = [], set()

    for name, tickers in TW_THEME_SECTORS.items():
        themed.update(tickers)
        stocks, tvol = [], 0
        for code in tickers:
            if code not in performance:
                continue
            p = performance[code]
            vol = p.get("volume", 0)
            tvol += vol
            stocks.append({"ticker": code, "name": TW_STOCK_LIST.get(code, p.get("name", code)),
                           "current_price": p["current_price"], "change_pct": p["change_pct"],
                           "volume": vol, "to_limit_pct": p.get("to_limit_pct"),
                           "change_5d_pct": p["change_pct"], "is_hot": p["change_pct"] >= 7})
        if len(stocks) >= 2:
            results.append(_calc_score(name, stocks, tvol))

    for big_cat, industries in OTHER_MAP.items():
        stocks, tvol = [], 0
        for ind in industries:
            for code in industry_map.get(ind, []):
                if code in themed or code not in performance:
                    continue
                p = performance[code]
                vol = p.get("volume", 0)
                tvol += vol
                stocks.append({"ticker": code, "name": stock_names.get(code, p.get("name", code)),
                               "current_price": p["current_price"], "change_pct": p["change_pct"],
                               "volume": vol, "to_limit_pct": p.get("to_limit_pct"),
                               "change_5d_pct": p["change_pct"], "is_hot": p["change_pct"] >= 7})
        if len(stocks) >= 2:
            results.append(_calc_score(big_cat, stocks, tvol))

    results.sort(key=lambda x: x["strength_score"], reverse=True)
    return {"date": datetime.now().strftime("%Y-%m-%d"), "market": "tw",
            "top_sectors": results[:5], "total_industries": len(results),
            "scanned_at": datetime.utcnow().isoformat()}

@router.get("/us")
async def scan_us():
    results = []
    for name, tickers in US_SECTORS.items():
        # FIX: asyncio.to_thread 避免同步 yfinance 阻塞 event loop
        r = await asyncio.to_thread(scan_sector_yf, tickers, "us")
        results.append({"sector": name, **r})
    results.sort(key=lambda x: x["avg_change_pct"], reverse=True)
    return {"date": datetime.now().strftime("%Y-%m-%d"), "market": "us",
            "top_sectors": results[:5],
            "strong_sectors": [s for s in results if s["avg_change_pct"] > 0][:5],
            "weak_sectors":   [s for s in results if s["avg_change_pct"] <= 0][:5],
            "scanned_at": datetime.utcnow().isoformat()}

@router.get("/alerts/{user_id}")
async def check_alerts(user_id: str):
    from database import db
    from services.yfinance_service import get_stock_data, calculate_status
    # FIX: 原本 {"name": user_id}，改為 {"email": user_id}
    user  = await db.users.find_one({"email": user_id})
    rules = user.get("rules", {}) if user else {"profit_trailing_pct": 20, "stoploss_pct": 7}
    alerts = []
    async for h in db.holdings.find({"user_id": user_id}):
        # FIX: asyncio.to_thread 避免阻塞
        live = await asyncio.to_thread(get_stock_data, h["ticker"], h["market"])
        if not live:
            continue
        current  = live["current_price"]
        highest  = max(live["highest_price"], h.get("highest_price", 0))
        entry    = h["entry_price"]
        pnl_pct  = round((current - entry) / entry * 100, 2)
        pullback = round((highest - current) / highest * 100, 2)
        pt = rules.get("profit_trailing_pct", 20)
        sl = rules.get("stoploss_pct", 7)
        name = h.get("name", h["ticker"])
        if pnl_pct > 0 and pullback >= pt:
            alerts.append({"type":"profit_alert","ticker":h["ticker"],"name":name,
                           "message":f"從最高點回檔 {pullback}%，已觸發停利條件","pnl_pct":pnl_pct,"severity":"high"})
        elif pnl_pct > 0 and pullback >= pt * 0.8:
            alerts.append({"type":"profit_watch","ticker":h["ticker"],"name":name,
                           "message":f"回檔 {pullback}%，接近 {pt}% 停利觸發線","pnl_pct":pnl_pct,"severity":"medium"})
        elif pnl_pct <= -sl:
            alerts.append({"type":"loss_alert","ticker":h["ticker"],"name":name,
                           "message":f"虧損 {abs(pnl_pct)}%，已觸發停損條件","pnl_pct":pnl_pct,"severity":"high"})
        if live.get("ma60_diff_pct") and live["ma60_diff_pct"] < -2:
            alerts.append({"type":"ma_alert","ticker":h["ticker"],"name":name,
                           "message":f"跌破季線 {abs(live['ma60_diff_pct'])}%","severity":"high"})
        elif live.get("ma20_diff_pct") and live["ma20_diff_pct"] < -1:
            alerts.append({"type":"ma_watch","ticker":h["ticker"],"name":name,
                           "message":f"接近月線，距離 {abs(live['ma20_diff_pct'])}%","severity":"medium"})
    return {"alerts": alerts, "checked_at": datetime.utcnow().isoformat()}

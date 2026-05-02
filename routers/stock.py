from fastapi import APIRouter, HTTPException
import httpx
from services.yfinance_service import get_stock_data, calculate_technical_indicators
from services.twse import _twse_cache

router = APIRouter(prefix="/stock", tags=["stock"])

# 靜態備用清單（TWSE 快取空時使用）
TW_STOCK_FALLBACK = {
    "2330":"台積電","2303":"聯電","5347":"世界先進","6770":"力積電",
    "2454":"聯發科","3034":"聯詠","2379":"瑞昱","6415":"矽力-KY","3661":"世芯-KY",
    "5269":"祥碩","5274":"信驊","6533":"晶心科","4919":"新唐","8081":"致新",
    "2408":"南亞科","2337":"旺宏","2344":"華邦電","8299":"群聯","4967":"十銓","6239":"力旺",
    "3711":"日月光投控","2449":"京元電子","6257":"矽格","8150":"南茂","6147":"頎邦",
    "6510":"精測","6515":"穎崴","2441":"超豐","6271":"同欣電","8131":"福懋科",
    "2369":"菱生","3265":"台星科","2329":"華泰","6261":"久元","3374":"精材",
    "2382":"廣達","2317":"鴻海","3231":"緯創","2356":"英業達","6669":"緯穎",
    "2301":"光寶科","2324":"仁寶","3706":"神達","2352":"佳世達","4938":"和碩",
    "3017":"奇鋐","4763":"材料-KY","8097":"常珵","6230":"超眾","3324":"雙鴻",
    "2421":"建準","3338":"泰碩","3189":"景碩","8046":"南電","6269":"台郡",
    "2383":"台光電","3037":"欣興","6274":"台燿","3044":"健鼎","2368":"金像電",
    "2327":"國巨","2492":"華新科","3026":"禾伸堂","2428":"興勤","2456":"奇力新",
    "6155":"鈞寶","2375":"凱美","2455":"全新光電","2478":"大毅","3624":"光頡",
    "2308":"台達電","6282":"康舒","2385":"群光","6138":"茂達","3035":"智原",
    "2345":"智邦","3596":"合勤","3380":"明泰","5388":"中磊","2332":"友訊",
    "4906":"正文","6285":"啟碁","3491":"昇達科",
    "3008":"大立光","3406":"玉晶光","3362":"先進光","3019":"亞光",
    "2409":"友達","3481":"群創","6116":"彩晶","3592":"瑞鼎",
    "2603":"長榮","2609":"陽明","2615":"萬海","2637":"慧洋-KY","2612":"中航",
    "2881":"富邦金","2882":"國泰金","2891":"中信金","2886":"兆豐金",
    "2884":"玉山金","2887":"台新金","2892":"第一金","5880":"合庫金",
    "2002":"中鋼","2015":"豐興","1301":"台塑","1303":"南亞","1326":"台化","6505":"台塑化",
    "1216":"統一","1201":"味全","1215":"卜蜂","1210":"大成","1218":"泰山",
    "2412":"中華電","3045":"台灣大","4904":"遠傳",
    "2610":"中華航","2618":"長榮航","2731":"雄獅","5706":"鳳凰","2707":"晶華",
    "3293":"鈊象","5478":"智冠","3546":"宇峻","6720":"凱鈿",
    "1101":"台泥","1102":"亞泥","2542":"興富發","5522":"遠雄",
    "2049":"上銀","1597":"直得","2395":"研華","2359":"所羅門","1560":"新代",
    "2201":"裕隆","2204":"中華車","2206":"三陽工業","6605":"帝寶",
    "2313":"華通","3836":"萊德光電","2314":"台揚","3163":"波若威",
    "6491":"采鈺","2376":"技嘉","2357":"華碩","2353":"宏碁",
    "6243":"迅杰","3234":"光環","6626":"華星光通","2474":"可成",
    "3023":"信邦","3533":"嘉澤","5457":"宣德","6290":"良維",
    "2207":"和泰車","2227":"裕日車","3665":"貿聯-KY","4551":"智伸科",
    "4152":"台微體","4174":"浩鼎","6499":"益安","4130":"健亞",
    "6443":"元晶","6244":"茂迪","3514":"昱晶","3691":"碩禾",
    "2458":"義隆","4966":"譜瑞-KY","3598":"奕力","2379":"瑞昱",
    "6223":"旺矽","3264":"欣銓","2449":"京元電子",
    # ETF
    "00631L":"元大台灣50正2","00632R":"元大台灣50反1",
    "00633L":"富邦台灣加權正2","00634R":"富邦台灣加權反1",
    "00670L":"富邦NASDAQ正2","00671R":"富邦NASDAQ反1",
    "00675L":"富邦臺灣加權正2","00637L":"元大滬深300正2",
    "00680L":"元大美債20正2","00688L":"國泰20年美債正2",
    "0050":"元大台灣50","0056":"元大高股息",
    "00878":"國泰永續高股息","00919":"群益台灣精選高息",
    "00929":"復華台灣科技優息","00940":"元大台灣價值高息",
}

@router.get("/search")
async def search_stock(q: str, market: str = "tw"):
    if market == "tw":
        q = q.strip()

        # 1. FinMind 搜尋（最完整，含上市上櫃興櫃）
        try:
            from services.finmind import search_tw_stock
            results = await search_tw_stock(q)
            if results:
                return results[:10]
        except Exception as e:
            print(f"FinMind search failed: {e}")

        # 2. TWSE 快取備用
        results, seen = [], set()
        cached_names = _twse_cache.get("stock_names", {})
        for code, name in cached_names.items():
            if q in code or q in name:
                results.append({"ticker": code, "name": name, "market": "tw"})
                seen.add(code)
                if len(results) >= 8:
                    break

        # 3. 靜態清單備用
        for code, name in TW_STOCK_FALLBACK.items():
            if code not in seen and (q in code or q in name):
                results.append({"ticker": code, "name": name, "market": "tw"})

        if results:
            return results[:10]

        if q.isdigit():
            return [{"ticker": q, "name": f"代號 {q}", "market": "tw"}]

    return [{"ticker": q.upper(), "name": q.upper(), "market": market}]

@router.get("/{market}/{ticker}")
async def get_stock_info(market: str, ticker: str):
    if market == "tw":
        from services.finmind import get_tw_stock_price
        data = await get_tw_stock_price(ticker)
        if data:
            return data
    data = get_stock_data(ticker, market)
    if not data:
        raise HTTPException(status_code=404, detail=f"找不到 {ticker}")
    return data

@router.get("/{market}/{ticker}/analysis")
async def get_technical_analysis(market: str, ticker: str):
    if market == "tw":
        from services.finmind import get_tw_technical
        data = await get_tw_technical(ticker)
        if data:
            return data
    data = calculate_technical_indicators(ticker, market)
    if not data:
        raise HTTPException(status_code=404, detail=f"無法分析 {ticker}")
    return data

"""
執行方式：
  cd ~/Desktop/tradeguard-backend/static
  python3 ../patch_html.py

這個腳本會自動把大盤雷達 tab 加進 index.html
"""
import os, sys, re

HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")
if not os.path.exists(HTML_PATH):
    # 如果從 static 資料夾裡執行
    HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
if not os.path.exists(HTML_PATH):
    print(f"找不到 index.html，請確認路徑")
    sys.exit(1)

with open(HTML_PATH, "r", encoding="utf-8") as f:
    html = f.read()

# ═══════════════════════════════════════════════════════════════
# 1. 加 Tab 按鈕（在股癌雷達 tab 後面）
# ═══════════════════════════════════════════════════════════════
TAB_MARKER = "股癌雷達</button>"
TAB_NEW = """股癌雷達</button>
      <button class="tab" onclick="go('market')">大盤雷達</button>"""

if "大盤雷達" not in html:
    if TAB_MARKER in html:
        html = html.replace(TAB_MARKER, TAB_NEW)
        print("✓ 加入大盤雷達 Tab 按鈕")
    else:
        print("✗ 找不到股癌雷達按鈕，請手動加 Tab 按鈕")

# ═══════════════════════════════════════════════════════════════
# 2. 加 Tab 內容 Section（在 settings section 前面）
# ═══════════════════════════════════════════════════════════════
SECTION_MARKER = "<!-- SETTINGS -->"
MARKET_SECTION = """<!-- MARKET RADAR -->
    <div id="market" class="section">

      <!-- 頂部：手動刷新 + 上次更新時間 -->
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div>
          <div style="font-size:15px;font-weight:700">大盤雷達</div>
          <div id="marketUpdatedAt" style="font-size:12px;color:#aaa;margin-top:2px">點擊刷新載入資料</div>
        </div>
        <button onclick="loadMarketRadar()" style="padding:8px 16px;background:#1a7a52;color:#fff;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer">刷新</button>
      </div>

      <!-- VIX + 大盤概況 -->
      <div id="marketVixRow" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px"></div>

      <!-- 國際指數 -->
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">國際指數</div>
        <div id="marketIndices" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
          <div class="loading"><span class="spinner"></span></div>
        </div>
      </div>

      <!-- 台灣市場 -->
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">台灣市場</div>
        <div id="marketTW" style="margin-top:8px">
          <div class="loading"><span class="spinner"></span></div>
        </div>
      </div>

      <!-- 族群強弱 -->
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">族群強弱（今日 Top 10）</div>
        <div id="marketSectors" style="margin-top:8px">
          <div class="loading"><span class="spinner"></span></div>
        </div>
      </div>

      <!-- 個股背離 -->
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">持倉背離偵測</div>
        <div id="marketDivergence" style="margin-top:8px">
          <div class="loading"><span class="spinner"></span></div>
        </div>
      </div>

      <!-- 融資維持率 -->
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">個人融資維持率</div>
        <div id="marketMarginHealth" style="margin-top:8px">
          <div class="loading"><span class="spinner"></span></div>
        </div>
      </div>

    </div>

    <!-- SETTINGS -->"""

if "<!-- MARKET RADAR -->" not in html:
    if SECTION_MARKER in html:
        html = html.replace(SECTION_MARKER, MARKET_SECTION)
        print("✓ 加入大盤雷達 Section HTML")
    else:
        print("✗ 找不到 <!-- SETTINGS --> 標記，請手動加 Section")

# ═══════════════════════════════════════════════════════════════
# 3. 加 tabIds（在 go() 函數的 tabIds 裡加 market）
# ═══════════════════════════════════════════════════════════════
TAB_IDS_OLD = "'gooaye'];"
TAB_IDS_NEW = "'gooaye','market'];"
if "'market'" not in html and TAB_IDS_OLD in html:
    html = html.replace(TAB_IDS_OLD, TAB_IDS_NEW)
    print("✓ 更新 tabIds 加入 market")

# ═══════════════════════════════════════════════════════════════
# 4. 加 go() 函數的 market 載入觸發
# ═══════════════════════════════════════════════════════════════
GO_GOOAYE = "if(t === 'gooaye') loadGooayeHistory();"
GO_MARKET = """if(t === 'gooaye') loadGooayeHistory();
  if(t === 'market') loadMarketRadar();"""
if "loadMarketRadar" not in html and GO_GOOAYE in html:
    html = html.replace(GO_GOOAYE, GO_MARKET)
    print("✓ 加入 market tab 觸發邏輯")

# ═══════════════════════════════════════════════════════════════
# 5. 加 JavaScript 函數（在 </script> 前）
# ═══════════════════════════════════════════════════════════════
MARKET_JS = """
// ==================== 大盤雷達 ====================

function renderChangeCell(pct) {
  const color = pct > 0 ? '#1a7a52' : pct < 0 ? '#d63b3b' : '#888';
  const arrow = pct > 0 ? '▲' : pct < 0 ? '▼' : '-';
  return `<span style="color:${color};font-weight:600">${arrow}${Math.abs(pct).toFixed(2)}%</span>`;
}

function renderRsiKd(rsi, kd) {
  const rsiColor = rsi > 70 ? '#d63b3b' : rsi < 30 ? '#1a7a52' : '#888';
  return `<span style="font-size:11px;color:#888">RSI <span style="color:${rsiColor}">${rsi}</span>  KD K${kd?.k??'-'}/D${kd?.d??'-'}</span>`;
}

function renderDivBadge(div) {
  if (!div || div.type === 'none') return '';
  const color = div.type === 'bearish' ? '#d63b3b' : '#1a7a52';
  const label = div.type === 'bearish' ? '頂背離' : '底背離';
  return `<div style="font-size:11px;color:${color};margin-top:4px;padding:3px 8px;background:${color}18;border-radius:6px;display:inline-block">${label}：${div.signal}</div>`;
}

async function loadMarketRadar() {
  if (!USER_ID) { alert('請先登入'); return; }
  document.getElementById('marketUpdatedAt').textContent = '載入中...';

  // 並行抓所有資料
  const [indicesRes, twRes, sectorsRes, divRes, marginRes] = await Promise.allSettled([
    fetch(`${API_BASE}/market/indices`).then(r=>r.json()),
    fetch(`${API_BASE}/market/tw`).then(r=>r.json()),
    fetch(`${API_BASE}/market/sectors`).then(r=>r.json()),
    fetch(`${API_BASE}/market/holdings-divergence/${USER_ID}`).then(r=>r.json()),
    fetch(`${API_BASE}/market/margin-health/${USER_ID}`).then(r=>r.json()),
  ]);

  document.getElementById('marketUpdatedAt').textContent =
    '更新時間：' + new Date().toLocaleTimeString('zh-TW');

  // VIX + 大盤概況
  if (indicesRes.status === 'fulfilled') {
    renderVixRow(indicesRes.value.indices || {});
    renderIndices(indicesRes.value.indices || {});
  }

  if (twRes.status === 'fulfilled') renderTWMarket(twRes.value);
  if (sectorsRes.status === 'fulfilled') renderSectors(sectorsRes.value.sectors || []);
  if (divRes.status === 'fulfilled') renderDivergence(divRes.value.stocks || []);
  if (marginRes.status === 'fulfilled') renderMarginHealth(marginRes.value.holdings || []);
}

function renderVixRow(indices) {
  const vix = indices.vix;
  const tw  = indices.sp500;  // placeholder
  if (!vix) return;

  const vixColor = vix.vix_status === 'danger' ? '#d63b3b'
                 : vix.vix_status === 'warning' ? '#c97c0a' : '#1a7a52';
  const vixLabel = vix.vix_status === 'danger' ? '【危險】'
                 : vix.vix_status === 'warning' ? '【注意】' : '【正常】';

  document.getElementById('marketVixRow').innerHTML = `
    <div class="card" style="text-align:center;border-left:3px solid ${vixColor}">
      <div style="font-size:12px;color:#aaa">VIX 恐慌指數</div>
      <div style="font-size:26px;font-weight:800;color:${vixColor}">${vix.current}</div>
      <div style="font-size:12px;color:${vixColor}">${vixLabel}</div>
      <div style="margin-top:4px">${renderChangeCell(vix.change_pct)}</div>
    </div>
    <div class="card" style="text-align:center">
      <div style="font-size:12px;color:#aaa">美元指數 DXY</div>
      <div style="font-size:22px;font-weight:700">${indices.dxy?.current?.toFixed(2) ?? '--'}</div>
      <div style="margin-top:4px">${indices.dxy ? renderChangeCell(indices.dxy.change_pct) : '--'}</div>
      <div style="margin-top:4px;font-size:12px;color:#888">美債10Y：${indices.us10y?.current?.toFixed(2) ?? '--'}%</div>
    </div>`;
}

function renderIndices(indices) {
  const ORDER = ['sp500','nasdaq','sox','nikkei','kospi','usdtwd'];
  const el = document.getElementById('marketIndices');
  el.innerHTML = ORDER.map(key => {
    const d = indices[key];
    if (!d) return `<div class="card" style="opacity:.4">${key}</div>`;
    const divBadge = renderDivBadge(d.divergence);
    return `<div class="card">
      <div style="font-size:12px;color:#aaa">${d.name}</div>
      <div style="font-size:18px;font-weight:700;margin:2px 0">${Number(d.current).toLocaleString()}</div>
      <div>${renderChangeCell(d.change_pct)}</div>
      <div style="margin-top:4px">${renderRsiKd(d.rsi, d.kd)}</div>
      ${divBadge}
    </div>`;
  }).join('');
}

function renderTWMarket(data) {
  const el = document.getElementById('marketTW');
  if (!data) { el.innerHTML = '<div style="color:#aaa;text-align:center">無法取得資料</div>'; return; }

  const { tw_index, tw_futures, margin, institutional, breadth } = data;
  const rows = [];

  if (tw_index) {
    rows.push(`<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">
      <div><span style="font-weight:600">加權指數</span></div>
      <div style="text-align:right">
        <span style="font-size:16px;font-weight:700">${Number(tw_index.current).toLocaleString()}</span>
        &nbsp;${renderChangeCell(tw_index.change_pct)}
        <div style="margin-top:2px">${renderRsiKd(tw_index.rsi, tw_index.kd)}</div>
      </div>
    </div>`);
  }

  if (tw_futures) {
    rows.push(`<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">
      <div><span style="font-weight:600">台指期 TX</span></div>
      <div>${Number(tw_futures.current).toLocaleString()} &nbsp;${renderChangeCell(tw_futures.change_pct)}</div>
    </div>`);
  }

  if (margin) {
    const tcolor = margin.change_pct > 0 ? '#d63b3b' : '#1a7a52';
    rows.push(`<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">
      <div><span style="font-weight:600">融資餘額</span></div>
      <div style="text-align:right">${margin.balance}億
        <span style="color:${tcolor};font-size:12px;margin-left:6px">${margin.trend} ${margin.change_pct > 0 ? '+' : ''}${margin.change_pct}%</span>
      </div>
    </div>`);
  }

  if (institutional) {
    const tcolor = institutional.foreign_net_5d > 0 ? '#1a7a52' : '#d63b3b';
    rows.push(`<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">
      <div><span style="font-weight:600">外資近5日</span></div>
      <div style="color:${tcolor};font-weight:600">${institutional.trend} ${institutional.foreign_net_5d}億</div>
    </div>`);
  }

  if (breadth) {
    const bcolor = breadth.ratio > 2 ? '#1a7a52' : breadth.ratio < 0.5 ? '#d63b3b' : '#888';
    rows.push(`<div style="display:flex;justify-content:space-between;padding:8px 0">
      <div><span style="font-weight:600">漲跌家數</span></div>
      <div style="text-align:right">
        <span style="color:#1a7a52">${breadth.up}↑</span>
        &nbsp;/&nbsp;
        <span style="color:#d63b3b">${breadth.down}↓</span>
        &nbsp;漲停${breadth.limit_up}/跌停${breadth.limit_down}
        <span style="font-size:12px;margin-left:6px;color:${bcolor}">[${breadth.breadth}]</span>
      </div>
    </div>`);
  }

  el.innerHTML = rows.join('') || '<div style="color:#aaa;text-align:center">無資料</div>';
}

function renderSectors(sectors) {
  const el = document.getElementById('marketSectors');
  if (!sectors.length) { el.innerHTML = '<div style="color:#aaa;text-align:center">無資料</div>'; return; }
  const top10 = sectors.slice(0, 10);
  el.innerHTML = top10.map((s, i) => {
    const barW = Math.min(s.strength_score * 100, 100);
    const chgColor = s.avg_change_pct > 0 ? '#1a7a52' : '#d63b3b';
    return `<div style="padding:7px 0;border-bottom:1px solid #f8f8f5">
      <div style="display:flex;justify-content:space-between;margin-bottom:4px">
        <span style="font-size:13px;font-weight:${i < 3 ? 700 : 400}">${i+1}. ${s.sector}</span>
        <span style="color:${chgColor};font-weight:600;font-size:13px">${s.avg_change_pct > 0 ? '+' : ''}${s.avg_change_pct}%</span>
      </div>
      <div style="height:4px;background:#f0f0ec;border-radius:2px">
        <div style="height:4px;width:${barW}%;background:${chgColor};border-radius:2px"></div>
      </div>
    </div>`;
  }).join('');
}

function renderDivergence(stocks) {
  const el = document.getElementById('marketDivergence');
  if (!stocks.length) { el.innerHTML = '<div style="color:#aaa;text-align:center;padding:8px">持倉資料載入中...</div>'; return; }
  const withDiv = stocks.filter(s => s.divergence?.type !== 'none');
  if (!withDiv.length) {
    el.innerHTML = '<div style="color:#1a7a52;text-align:center;padding:8px">目前無背離訊號</div>';
    return;
  }
  el.innerHTML = withDiv.map(s => {
    const color = s.divergence.type === 'bearish' ? '#d63b3b' : '#1a7a52';
    return `<div style="padding:10px 0;border-bottom:1px solid #f0f0ec">
      <div style="display:flex;justify-content:space-between">
        <span style="font-weight:600">${s.ticker} ${s.name}</span>
        <span style="color:${color};font-size:12px;font-weight:600">${s.divergence.type === 'bearish' ? '頂背離' : '底背離'}</span>
      </div>
      <div style="font-size:12px;color:#666;margin-top:4px">${s.divergence.signal}</div>
      <div style="font-size:12px;color:#aaa;margin-top:2px">現價 $${s.current} | ${renderRsiKd(s.rsi, s.kd)} | ${s.direction}</div>
    </div>`;
  }).join('');
}

function renderMarginHealth(holdings) {
  const el = document.getElementById('marketMarginHealth');
  if (!holdings.length) {
    el.innerHTML = '<div style="color:#aaa;text-align:center;padding:8px">無融資持倉</div>';
    return;
  }
  el.innerHTML = holdings.map(h => {
    const color = h.status === 'danger' ? '#d63b3b' : h.status === 'warning' ? '#c97c0a' : '#1a7a52';
    const barW  = Math.min(Math.max((h.maintenance_pct - 100) / 2, 0), 100);
    return `<div style="padding:10px 0;border-bottom:1px solid #f0f0ec">
      <div style="display:flex;justify-content:space-between;margin-bottom:6px">
        <span style="font-weight:600">${h.ticker} ${h.name}</span>
        <span style="color:${color};font-weight:700">${h.maintenance_pct}%</span>
      </div>
      <div style="height:6px;background:#f0f0ec;border-radius:3px;margin-bottom:4px">
        <div style="height:6px;width:${barW}%;background:${color};border-radius:3px;transition:width 0.5s"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:#888">
        <span>追繳線：$${h.call_price}（跌破此價觸發）</span>
        <span>現價 $${h.current}</span>
      </div>
    </div>`;
  }).join('');
}

"""

if "loadMarketRadar" not in html:
    if "</script>" in html:
        html = html.replace("</script>", MARKET_JS + "</script>", 1)
        print("✓ 加入大盤雷達 JavaScript 函數")
    else:
        print("✗ 找不到 </script>，請手動加入 JS")
else:
    print("! loadMarketRadar 已存在，跳過 JS 加入")

# ═══════════════════════════════════════════════════════════════
# 寫回檔案
# ═══════════════════════════════════════════════════════════════
with open(HTML_PATH, "w", encoding="utf-8") as f:
    f.write(html)

print(f"\n完成！{HTML_PATH} 已更新")
print("現在可以執行 git add static/index.html 推上去了")

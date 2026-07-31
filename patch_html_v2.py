"""
執行方式：
  cd ~/Desktop/tradeguard-backend
  python3 patch_html_v2.py
"""
import os, sys

HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")
if not os.path.exists(HTML_PATH):
    HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")
if not os.path.exists(HTML_PATH):
    print("找不到 index.html"); sys.exit(1)

with open(HTML_PATH, "r", encoding="utf-8") as f:
    html = f.read()

# 如果已經做過就跳過
if "loadMarketRadar" in html:
    print("! 大盤雷達已存在，不重複加入")
    sys.exit(0)

INJECT = """
<!-- ===== 大盤雷達：動態注入 ===== -->
<script>
(function(){
  // 1. 建立 #market section div（如果不存在）
  if (!document.getElementById('market')) {
    var sec = document.createElement('div');
    sec.id = 'market';
    sec.className = 'section';
    sec.style.display = 'none';
    sec.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div>
          <div style="font-size:15px;font-weight:700">大盤雷達</div>
          <div id="marketUpdatedAt" style="font-size:12px;color:#aaa;margin-top:2px">點擊刷新載入資料</div>
        </div>
        <button onclick="loadMarketRadar()" style="padding:8px 16px;background:#1a7a52;color:#fff;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer">刷新</button>
      </div>
      <div id="marketVixRow" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px"></div>
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">國際指數</div>
        <div id="marketIndices" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
          <div style="color:#aaa;text-align:center;padding:12px;grid-column:1/-1">點擊刷新載入</div>
        </div>
      </div>
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">台灣市場</div>
        <div id="marketTW" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新載入</div></div>
      </div>
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">族群強弱（Top 10）</div>
        <div id="marketSectors" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新載入</div></div>
      </div>
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">持倉背離偵測</div>
        <div id="marketDivergence" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新載入</div></div>
      </div>
      <div class="card" style="margin-bottom:10px">
        <div class="card-title">個人融資維持率</div>
        <div id="marketMarginHealth" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新載入</div></div>
      </div>`;
    document.body.appendChild(sec);
  }

  // 2. 把 market 加入 tabIds（如果還沒有）
  if (typeof tabIds !== 'undefined' && !tabIds.includes('market')) {
    tabIds.push('market');
  }

  // 3. Patch go() 函數，加入 market 的觸發
  var origGo = window.go;
  window.go = function(t) {
    origGo(t);
    if (t === 'market') loadMarketRadar();
  };
})();

// ==================== 大盤雷達函數 ====================

function _renderChange(pct) {
  var color = pct > 0 ? '#1a7a52' : pct < 0 ? '#d63b3b' : '#888';
  var arrow = pct > 0 ? '▲' : pct < 0 ? '▼' : '-';
  return '<span style="color:' + color + ';font-weight:600">' + arrow + Math.abs(pct).toFixed(2) + '%</span>';
}

function _renderRsiKd(rsi, kd) {
  var rc = rsi > 70 ? '#d63b3b' : rsi < 30 ? '#1a7a52' : '#888';
  return '<span style="font-size:11px;color:#888">RSI <span style="color:' + rc + '">' + rsi + '</span>  K' + (kd&&kd.k||'-') + '/D' + (kd&&kd.d||'-') + '</span>';
}

function _renderDivBadge(div) {
  if (!div || div.type === 'none') return '';
  var color = div.type === 'bearish' ? '#d63b3b' : '#1a7a52';
  var label = div.type === 'bearish' ? '頂背離' : '底背離';
  return '<div style="font-size:11px;color:' + color + ';margin-top:4px;padding:3px 8px;background:' + color + '18;border-radius:6px;display:inline-block">' + label + '：' + div.signal + '</div>';
}

async function loadMarketRadar() {
  if (typeof USER_ID === 'undefined' || !USER_ID) { alert('請先登入'); return; }
  var updEl = document.getElementById('marketUpdatedAt');
  if (updEl) updEl.textContent = '載入中...';

  var [indRes, twRes, secRes, divRes, mhRes] = await Promise.allSettled([
    fetch(API_BASE + '/market/indices').then(r=>r.json()),
    fetch(API_BASE + '/market/tw').then(r=>r.json()),
    fetch(API_BASE + '/market/sectors').then(r=>r.json()),
    fetch(API_BASE + '/market/holdings-divergence/' + USER_ID).then(r=>r.json()),
    fetch(API_BASE + '/market/margin-health/' + USER_ID).then(r=>r.json()),
  ]);

  if (updEl) updEl.textContent = '更新時間：' + new Date().toLocaleTimeString('zh-TW');

  if (indRes.status === 'fulfilled') {
    _renderVixRow(indRes.value.indices || {});
    _renderIndices(indRes.value.indices || {});
  }
  if (twRes.status === 'fulfilled')  _renderTWMarket(twRes.value);
  if (secRes.status === 'fulfilled') _renderSectors((secRes.value.sectors || []));
  if (divRes.status === 'fulfilled') _renderDivergence((divRes.value.stocks || []));
  if (mhRes.status === 'fulfilled')  _renderMarginHealth((mhRes.value.holdings || []));
}

function _renderVixRow(idx) {
  var el = document.getElementById('marketVixRow');
  if (!el || !idx.vix) return;
  var v = idx.vix;
  var vc = v.vix_status === 'danger' ? '#d63b3b' : v.vix_status === 'warning' ? '#c97c0a' : '#1a7a52';
  var vl = v.vix_status === 'danger' ? '危險' : v.vix_status === 'warning' ? '注意' : '正常';
  el.innerHTML =
    '<div class="card" style="text-align:center;border-left:3px solid ' + vc + '">' +
      '<div style="font-size:12px;color:#aaa">VIX 恐慌指數</div>' +
      '<div style="font-size:26px;font-weight:800;color:' + vc + '">' + v.current + '</div>' +
      '<div style="font-size:12px;color:' + vc + '">【' + vl + '】</div>' +
      '<div style="margin-top:4px">' + _renderChange(v.change_pct) + '</div>' +
    '</div>' +
    '<div class="card" style="text-align:center">' +
      '<div style="font-size:12px;color:#aaa">美元指數 DXY</div>' +
      '<div style="font-size:22px;font-weight:700">' + (idx.dxy ? idx.dxy.current.toFixed(2) : '--') + '</div>' +
      '<div style="margin-top:4px">' + (idx.dxy ? _renderChange(idx.dxy.change_pct) : '--') + '</div>' +
      '<div style="font-size:12px;color:#888;margin-top:4px">美債10Y：' + (idx.us10y ? idx.us10y.current.toFixed(2) : '--') + '%</div>' +
    '</div>';
}

function _renderIndices(idx) {
  var el = document.getElementById('marketIndices');
  if (!el) return;
  var order = ['sp500','nasdaq','sox','nikkei','kospi','usdtwd'];
  el.innerHTML = order.map(function(key) {
    var d = idx[key];
    if (!d) return '<div class="card" style="opacity:.4">' + key + '</div>';
    return '<div class="card">' +
      '<div style="font-size:12px;color:#aaa">' + d.name + '</div>' +
      '<div style="font-size:18px;font-weight:700;margin:2px 0">' + Number(d.current).toLocaleString() + '</div>' +
      '<div>' + _renderChange(d.change_pct) + '</div>' +
      '<div style="margin-top:4px">' + _renderRsiKd(d.rsi, d.kd) + '</div>' +
      _renderDivBadge(d.divergence) +
    '</div>';
  }).join('');
}

function _renderTWMarket(data) {
  var el = document.getElementById('marketTW');
  if (!el || !data) return;
  var rows = [];
  var tw = data.tw_index, fut = data.tw_futures, mg = data.margin, inst = data.institutional, br = data.breadth;
  if (tw) rows.push(
    '<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">' +
    '<div><span style="font-weight:600">加權指數</span></div>' +
    '<div style="text-align:right">' +
      '<span style="font-size:16px;font-weight:700">' + Number(tw.current).toLocaleString() + '</span> ' +
      _renderChange(tw.change_pct) +
      '<div style="margin-top:2px">' + _renderRsiKd(tw.rsi, tw.kd) + '</div>' +
    '</div></div>');
  if (fut) rows.push(
    '<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">' +
    '<div><span style="font-weight:600">台指期 TX</span></div>' +
    '<div>' + Number(fut.current).toLocaleString() + ' ' + _renderChange(fut.change_pct) + '</div></div>');
  if (mg) {
    var tc = mg.change_pct > 0 ? '#d63b3b' : '#1a7a52';
    rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">' +
    '<div><span style="font-weight:600">融資餘額</span></div>' +
    '<div>' + mg.balance + '億 <span style="color:' + tc + ';font-size:12px">' + mg.trend + ' ' + (mg.change_pct > 0?'+':'') + mg.change_pct + '%</span></div></div>');
  }
  if (inst) {
    var ic = inst.foreign_net_5d > 0 ? '#1a7a52' : '#d63b3b';
    rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec">' +
    '<div><span style="font-weight:600">外資近5日</span></div>' +
    '<div style="color:' + ic + ';font-weight:600">' + inst.trend + ' ' + inst.foreign_net_5d + '億</div></div>');
  }
  if (br) {
    var bc = br.ratio > 2 ? '#1a7a52' : br.ratio < 0.5 ? '#d63b3b' : '#888';
    rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0">' +
    '<div><span style="font-weight:600">漲跌家數</span></div>' +
    '<div><span style="color:#1a7a52">' + br.up + '↑</span> / <span style="color:#d63b3b">' + br.down + '↓</span>' +
    '  漲停' + br.limit_up + '/跌停' + br.limit_down +
    ' <span style="color:' + bc + ';font-size:12px">[' + br.breadth + ']</span></div></div>');
  }
  el.innerHTML = rows.join('') || '<div style="color:#aaa;text-align:center;padding:12px">無資料</div>';
}

function _renderSectors(sectors) {
  var el = document.getElementById('marketSectors');
  if (!el) return;
  if (!sectors.length) { el.innerHTML = '<div style="color:#aaa;text-align:center;padding:12px">無資料</div>'; return; }
  el.innerHTML = sectors.slice(0,10).map(function(s, i) {
    var bw = Math.min(s.strength_score * 100, 100);
    var cc = s.avg_change_pct > 0 ? '#1a7a52' : '#d63b3b';
    return '<div style="padding:7px 0;border-bottom:1px solid #f8f8f5">' +
      '<div style="display:flex;justify-content:space-between;margin-bottom:4px">' +
      '<span style="font-size:13px;font-weight:' + (i<3?700:400) + '">' + (i+1) + '. ' + s.sector + '</span>' +
      '<span style="color:' + cc + ';font-weight:600;font-size:13px">' + (s.avg_change_pct>0?'+':'') + s.avg_change_pct + '%</span></div>' +
      '<div style="height:4px;background:#f0f0ec;border-radius:2px">' +
      '<div style="height:4px;width:' + bw + '%;background:' + cc + ';border-radius:2px"></div></div></div>';
  }).join('');
}

function _renderDivergence(stocks) {
  var el = document.getElementById('marketDivergence');
  if (!el) return;
  var withDiv = stocks.filter(function(s){ return s.divergence && s.divergence.type !== 'none'; });
  if (!withDiv.length) { el.innerHTML = '<div style="color:#1a7a52;text-align:center;padding:12px">目前無背離訊號</div>'; return; }
  el.innerHTML = withDiv.map(function(s) {
    var color = s.divergence.type === 'bearish' ? '#d63b3b' : '#1a7a52';
    return '<div style="padding:10px 0;border-bottom:1px solid #f0f0ec">' +
      '<div style="display:flex;justify-content:space-between">' +
      '<span style="font-weight:600">' + s.ticker + ' ' + s.name + '</span>' +
      '<span style="color:' + color + ';font-size:12px;font-weight:600">' + (s.divergence.type==='bearish'?'頂背離':'底背離') + '</span></div>' +
      '<div style="font-size:12px;color:#666;margin-top:4px">' + s.divergence.signal + '</div>' +
      '<div style="font-size:12px;color:#aaa;margin-top:2px">現價 $' + s.current + ' | ' + _renderRsiKd(s.rsi, s.kd) + ' | ' + s.direction + '</div></div>';
  }).join('');
}

function _renderMarginHealth(holdings) {
  var el = document.getElementById('marketMarginHealth');
  if (!el) return;
  if (!holdings.length) { el.innerHTML = '<div style="color:#aaa;text-align:center;padding:12px">無融資持倉</div>'; return; }
  el.innerHTML = holdings.map(function(h) {
    var color = h.status === 'danger' ? '#d63b3b' : h.status === 'warning' ? '#c97c0a' : '#1a7a52';
    var bw = Math.min(Math.max((h.maintenance_pct - 100) / 2, 0), 100);
    return '<div style="padding:10px 0;border-bottom:1px solid #f0f0ec">' +
      '<div style="display:flex;justify-content:space-between;margin-bottom:6px">' +
      '<span style="font-weight:600">' + h.ticker + ' ' + h.name + '</span>' +
      '<span style="color:' + color + ';font-weight:700">' + h.maintenance_pct + '%</span></div>' +
      '<div style="height:6px;background:#f0f0ec;border-radius:3px;margin-bottom:4px">' +
      '<div style="height:6px;width:' + bw + '%;background:' + color + ';border-radius:3px"></div></div>' +
      '<div style="display:flex;justify-content:space-between;font-size:11px;color:#888">' +
      '<span>追繳線：$' + h.call_price + '</span><span>現價 $' + h.current + '</span></div></div>';
  }).join('');
}
</script>
"""

# 在 </body> 前插入
if "</body>" in html:
    html = html.replace("</body>", INJECT + "</body>")
    print("✓ 大盤雷達注入成功")
else:
    html += INJECT
    print("✓ 大盤雷達附加到檔案末尾")

with open(HTML_PATH, "w", encoding="utf-8") as f:
    f.write(html)

print(f"完成！{HTML_PATH} 已更新")
print("執行：git add static/index.html && git commit -m 'feat: 大盤雷達' && git push")

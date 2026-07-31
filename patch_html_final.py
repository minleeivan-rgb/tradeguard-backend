"""
執行：
  cd ~/Desktop/tradeguard-backend
  python3 patch_html_final.py
"""
import os, sys

HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "index.html")
if not os.path.exists(HTML_PATH):
    print("找不到 static/index.html"); sys.exit(1)

with open(HTML_PATH, "r", encoding="utf-8") as f:
    html = f.read()

changed = False

# ── 1. Tab 按鈕 ────────────────────────────────────────────────
if "大盤雷達" not in html:
    for marker in ["股癌雷達</button>", "gooaye</button>", "股癌雷達</a>"]:
        if marker in html:
            html = html.replace(marker, marker + '\n      <button class="tab" onclick="go(\'market\')">大盤雷達</button>')
            print("✓ Tab 按鈕加入")
            changed = True
            break
    else:
        print("! 找不到股癌雷達按鈕，Tab 按鈕未加入（不影響功能）")
else:
    print("· Tab 按鈕已存在")

# ── 2. tabIds 加 market ─────────────────────────────────────────
if "'market'" not in html and '"market"' not in html:
    for old, new in [("'gooaye'];", "'gooaye','market'];"), ('"gooaye"];', '"gooaye","market"];')]:
        if old in html:
            html = html.replace(old, new)
            print("✓ tabIds 加入 market")
            changed = True
            break
else:
    print("· tabIds 已包含 market")

# ── 3. go() 觸發 ────────────────────────────────────────────────
if "loadMarketRadar" not in html:
    for marker in ["if(t === 'gooaye') loadGooayeHistory();",
                   "if(t==='gooaye') loadGooayeHistory();"]:
        if marker in html:
            html = html.replace(marker, marker + "\n  if(t === 'market') loadMarketRadar();")
            print("✓ go() 觸發加入")
            changed = True
            break
else:
    print("· go() 觸發已存在")

# ── 4. 所有 JS + #market div（在 </body> 前插入）──────────────
ALREADY_HAS_DIV = 'id="market"' in html or "id='market'" in html
ALREADY_HAS_JS  = "loadMarketRadar" in html

if ALREADY_HAS_DIV and ALREADY_HAS_JS:
    print("· market div 和 JS 已存在")
else:
    INJECT_PARTS = []

    if not ALREADY_HAS_DIV:
        INJECT_PARTS.append("""<script>
(function(){
  if(document.getElementById('market')) return;
  var s=document.createElement('div');
  s.id='market'; s.className='section'; s.style.display='none';
  s.innerHTML=
    '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">'+
      '<div><div style="font-size:15px;font-weight:700">大盤雷達</div>'+
      '<div id="marketUpdatedAt" style="font-size:12px;color:#aaa;margin-top:2px">點擊刷新載入資料</div></div>'+
      '<button onclick="loadMarketRadar()" style="padding:8px 16px;background:#1a7a52;color:#fff;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer">刷新</button>'+
    '</div>'+
    '<div id="marketVixRow" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px"></div>'+
    '<div class="card" style="margin-bottom:10px"><div class="card-title">國際指數</div>'+
      '<div id="marketIndices" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">'+
      '<div style="color:#aaa;text-align:center;padding:12px;grid-column:1/-1">點擊刷新</div></div></div>'+
    '<div class="card" style="margin-bottom:10px"><div class="card-title">台灣市場</div>'+
      '<div id="marketTW" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新</div></div></div>'+
    '<div class="card" style="margin-bottom:10px"><div class="card-title">族群強弱 Top 10</div>'+
      '<div id="marketSectors" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新</div></div></div>'+
    '<div class="card" style="margin-bottom:10px"><div class="card-title">持倉背離偵測</div>'+
      '<div id="marketDivergence" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新</div></div></div>'+
    '<div class="card" style="margin-bottom:10px"><div class="card-title">個人融資維持率</div>'+
      '<div id="marketMarginHealth" style="margin-top:8px"><div style="color:#aaa;text-align:center;padding:12px">點擊刷新</div></div></div>';
  document.body.appendChild(s);
  if(typeof tabIds!=='undefined' && !tabIds.includes('market')) tabIds.push('market');
})();
</script>""")
        print("✓ #market div 注入")
        changed = True

    if not ALREADY_HAS_JS:
        INJECT_PARTS.append("""<script>
function _rc(p){var c=p>0?'#1a7a52':p<0?'#d63b3b':'#888',a=p>0?'▲':p<0?'▼':'-';return'<span style="color:'+c+';font-weight:600">'+a+Math.abs(p).toFixed(2)+'%</span>';}
function _rk(r,k){var rc=r>70?'#d63b3b':r<30?'#1a7a52':'#888';return'<span style="font-size:11px;color:#888">RSI <span style="color:'+rc+'">'+r+'</span> K'+(k&&k.k||'-')+'/D'+(k&&k.d||'-')+'</span>';}
function _rdiv(d){if(!d||d.type==='none')return'';var c=d.type==='bearish'?'#d63b3b':'#1a7a52',l=d.type==='bearish'?'頂背離':'底背離';return'<div style="font-size:11px;color:'+c+';margin-top:4px;padding:3px 8px;background:'+c+'18;border-radius:6px;display:inline-block">'+l+'：'+d.signal+'</div>';}

async function loadMarketRadar(){
  if(typeof USER_ID==='undefined'||!USER_ID){alert('請先登入');return;}
  var u=document.getElementById('marketUpdatedAt');
  if(u) u.textContent='載入中...';
  var [a,b,c,d,e]=await Promise.allSettled([
    fetch(API_BASE+'/market/indices').then(r=>r.json()),
    fetch(API_BASE+'/market/tw').then(r=>r.json()),
    fetch(API_BASE+'/market/sectors').then(r=>r.json()),
    fetch(API_BASE+'/market/holdings-divergence/'+USER_ID).then(r=>r.json()),
    fetch(API_BASE+'/market/margin-health/'+USER_ID).then(r=>r.json()),
  ]);
  if(u) u.textContent='更新：'+new Date().toLocaleTimeString('zh-TW');
  if(a.status==='fulfilled'){_rvix(a.value.indices||{});_ridx(a.value.indices||{});}
  if(b.status==='fulfilled') _rtw(b.value);
  if(c.status==='fulfilled') _rsec(c.value.sectors||[]);
  if(d.status==='fulfilled') _rdivs(d.value.stocks||[]);
  if(e.status==='fulfilled') _rmh(e.value.holdings||[]);
}

function _rvix(x){
  var el=document.getElementById('marketVixRow'); if(!el||!x.vix)return;
  var v=x.vix,vc=v.vix_status==='danger'?'#d63b3b':v.vix_status==='warning'?'#c97c0a':'#1a7a52',vl=v.vix_status==='danger'?'危險':v.vix_status==='warning'?'注意':'正常';
  el.innerHTML='<div class="card" style="text-align:center;border-left:3px solid '+vc+'"><div style="font-size:12px;color:#aaa">VIX 恐慌指數</div><div style="font-size:26px;font-weight:800;color:'+vc+'">'+v.current+'</div><div style="font-size:12px;color:'+vc+'">【'+vl+'】</div><div style="margin-top:4px">'+_rc(v.change_pct)+'</div></div>'+
  '<div class="card" style="text-align:center"><div style="font-size:12px;color:#aaa">美元指數 DXY</div><div style="font-size:22px;font-weight:700">'+(x.dxy?x.dxy.current.toFixed(2):'--')+'</div><div style="margin-top:4px">'+(x.dxy?_rc(x.dxy.change_pct):'--')+'</div><div style="font-size:12px;color:#888;margin-top:4px">美債10Y：'+(x.us10y?x.us10y.current.toFixed(2):'--')+'%</div></div>';
}

function _ridx(x){
  var el=document.getElementById('marketIndices'); if(!el)return;
  el.innerHTML=['sp500','nasdaq','sox','nikkei','kospi','usdtwd'].map(function(k){
    var d=x[k]; if(!d)return'<div class="card" style="opacity:.4">'+k+'</div>';
    return'<div class="card"><div style="font-size:12px;color:#aaa">'+d.name+'</div><div style="font-size:18px;font-weight:700;margin:2px 0">'+Number(d.current).toLocaleString()+'</div><div>'+_rc(d.change_pct)+'</div><div style="margin-top:4px">'+_rk(d.rsi,d.kd)+'</div>'+_rdiv(d.divergence)+'</div>';
  }).join('');
}

function _rtw(data){
  var el=document.getElementById('marketTW'); if(!el||!data)return;
  var rows=[],tw=data.tw_index,fut=data.tw_futures,mg=data.margin,inst=data.institutional,br=data.breadth;
  if(tw) rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec"><div><span style="font-weight:600">加權指數</span></div><div style="text-align:right"><span style="font-size:16px;font-weight:700">'+Number(tw.current).toLocaleString()+'</span> '+_rc(tw.change_pct)+'<div style="margin-top:2px">'+_rk(tw.rsi,tw.kd)+'</div></div></div>');
  if(fut) rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec"><div><span style="font-weight:600">台指期 TX</span></div><div>'+Number(fut.current).toLocaleString()+' '+_rc(fut.change_pct)+'</div></div>');
  if(mg){var tc=mg.change_pct>0?'#d63b3b':'#1a7a52';rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec"><div><span style="font-weight:600">融資餘額</span></div><div>'+mg.balance+'億 <span style="color:'+tc+';font-size:12px">'+mg.trend+' '+(mg.change_pct>0?'+':'')+mg.change_pct+'%</span></div></div>');}
  if(inst){var ic=inst.foreign_net_5d>0?'#1a7a52':'#d63b3b';rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0ec"><div><span style="font-weight:600">外資近5日</span></div><div style="color:'+ic+';font-weight:600">'+inst.trend+' '+inst.foreign_net_5d+'億</div></div>');}
  if(br){var bc=br.ratio>2?'#1a7a52':br.ratio<0.5?'#d63b3b':'#888';rows.push('<div style="display:flex;justify-content:space-between;padding:8px 0"><div><span style="font-weight:600">漲跌家數</span></div><div><span style="color:#1a7a52">'+br.up+'↑</span>/<span style="color:#d63b3b">'+br.down+'↓</span> 漲停'+br.limit_up+'/跌停'+br.limit_down+' <span style="color:'+bc+';font-size:12px">['+br.breadth+']</span></div></div>');}
  el.innerHTML=rows.join('')||'<div style="color:#aaa;text-align:center;padding:12px">無資料</div>';
}

function _rsec(s){
  var el=document.getElementById('marketSectors'); if(!el)return;
  if(!s.length){el.innerHTML='<div style="color:#aaa;text-align:center;padding:12px">無資料</div>';return;}
  el.innerHTML=s.slice(0,10).map(function(x,i){var bw=Math.min(x.strength_score*100,100),cc=x.avg_change_pct>0?'#1a7a52':'#d63b3b';return'<div style="padding:7px 0;border-bottom:1px solid #f8f8f5"><div style="display:flex;justify-content:space-between;margin-bottom:4px"><span style="font-size:13px;font-weight:'+(i<3?700:400)+'">'+(i+1)+'. '+x.sector+'</span><span style="color:'+cc+';font-weight:600;font-size:13px">'+(x.avg_change_pct>0?'+':'')+x.avg_change_pct+'%</span></div><div style="height:4px;background:#f0f0ec;border-radius:2px"><div style="height:4px;width:'+bw+'%;background:'+cc+';border-radius:2px"></div></div></div>';}).join('');
}

function _rdivs(s){
  var el=document.getElementById('marketDivergence'); if(!el)return;
  var w=s.filter(function(x){return x.divergence&&x.divergence.type!=='none';});
  if(!w.length){el.innerHTML='<div style="color:#1a7a52;text-align:center;padding:12px">目前無背離訊號</div>';return;}
  el.innerHTML=w.map(function(x){var c=x.divergence.type==='bearish'?'#d63b3b':'#1a7a52';return'<div style="padding:10px 0;border-bottom:1px solid #f0f0ec"><div style="display:flex;justify-content:space-between"><span style="font-weight:600">'+x.ticker+' '+x.name+'</span><span style="color:'+c+';font-size:12px;font-weight:600">'+(x.divergence.type==='bearish'?'頂背離':'底背離')+'</span></div><div style="font-size:12px;color:#666;margin-top:4px">'+x.divergence.signal+'</div><div style="font-size:12px;color:#aaa;margin-top:2px">現價 $'+x.current+' | '+_rk(x.rsi,x.kd)+' | '+x.direction+'</div></div>';}).join('');
}

function _rmh(h){
  var el=document.getElementById('marketMarginHealth'); if(!el)return;
  if(!h.length){el.innerHTML='<div style="color:#aaa;text-align:center;padding:12px">無融資持倉</div>';return;}
  el.innerHTML=h.map(function(x){var c=x.status==='danger'?'#d63b3b':x.status==='warning'?'#c97c0a':'#1a7a52',bw=Math.min(Math.max((x.maintenance_pct-100)/2,0),100);return'<div style="padding:10px 0;border-bottom:1px solid #f0f0ec"><div style="display:flex;justify-content:space-between;margin-bottom:6px"><span style="font-weight:600">'+x.ticker+' '+x.name+'</span><span style="color:'+c+';font-weight:700">'+x.maintenance_pct+'%</span></div><div style="height:6px;background:#f0f0ec;border-radius:3px;margin-bottom:4px"><div style="height:6px;width:'+bw+'%;background:'+c+';border-radius:3px"></div></div><div style="display:flex;justify-content:space-between;font-size:11px;color:#888"><span>追繳線：$'+x.call_price+'</span><span>現價 $'+x.current+'</span></div></div>';}).join('');
}
</script>""")
        print("✓ loadMarketRadar JS 注入")
        changed = True

    if INJECT_PARTS:
        inject_str = "\n".join(INJECT_PARTS)
        if "</body>" in html:
            html = html.replace("</body>", inject_str + "\n</body>")
        else:
            html += inject_str

if not changed:
    print("! 所有內容已存在，無需更新")
    sys.exit(0)

with open(HTML_PATH, "w", encoding="utf-8") as f:
    f.write(html)

print("\n完成！執行：")
print("git add static/index.html")
print('git commit -m "feat: 大盤雷達完整注入"')
print("git push")

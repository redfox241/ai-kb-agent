const $ = id => document.getElementById(id);
const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const grade = n => n>=8?['good','优秀']:(n>=6?['mid','中等']:['bad','待改进']);
const cls = n => n>=8?'sc-good':(n>=6?'sc-mid':'sc-bad');
const tag = n => { const [c,l]=grade(n); return `<span class="tag tg-${c}">${l} ${n.toFixed(1)}</span>`; };
const fmtMs = ms => ms==null?'-':(ms>=1000?(ms/1000).toFixed(1)+'s':ms+'ms');

/* ---------- 标签页 ---------- */
const loaded = {};
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b === btn));
    ['overview','visits','evals'].forEach(t => $('tab-'+t).hidden = (t !== btn.dataset.t));
    const t = btn.dataset.t;
    if (!loaded[t]) { loaded[t] = true; ({overview:loadOverview, visits:()=>loadVisits(true), evals:()=>loadEvals(true)})[t](); }
  });
});

/* ---------- 索引管理 ---------- */
async function loadIndexInfo() {
  try {
    const h = await fetch('/health').then(r=>r.json());
    let extra = '';
    if (h.index && h.index !== 'ready') extra += ` · 状态 <b>${esc(h.index)}</b>`;
    try {
      const rep = await fetch('/index_report').then(r=>r.json());
      if (rep.error) extra += ` · <span style="color:var(--bad)">${esc(rep.error)}</span>`;
    } catch {}
    $('idxInfo').innerHTML = `chunks <b>${h.n_chunks}</b> · built_at ${h.built_at || '-'}${extra}`;
  } catch(e) { $('idxInfo').textContent = '索引状态获取失败'; }
}

async function reindex() {
  const btn = $('reindexBtn'), msg = $('idxMsg'), bar = $('idxBar');
  btn.disabled = true; msg.style.display = 'inline'; msg.textContent = '触发重建…';
  try {
    const r = await fetch('/reindex', {method:'POST'}).then(r=>r.json());
    msg.textContent = r.status === 'already_building' ? '已有重建任务进行中' : '重建已启动';
    await pollIndex(bar, msg);
  } catch(e) {
    msg.textContent = '重建失败: ' + e.message;
  }
  btn.disabled = false;
}

async function pollIndex(bar, msg) {
  bar.style.display = 'block';
  while (true) {
    await new Promise(r=>setTimeout(r, 2000));
    let s;
    try { s = await fetch('/reindex_status').then(r=>r.json()); }
    catch(e) { msg.textContent = '状态查询失败: ' + e.message; return; }
    const ri = s.reindex || {};
    if (ri.status === 'done' || ri.status === 'failed') {
      bar.style.display = 'none';
      if (ri.status === 'done') {
        const h = await fetch('/health').then(r=>r.json());
        msg.textContent = `完成！chunks ${h.n_chunks} · built_at ${h.built_at || '-'}`;
        loadIndexInfo();
      } else {
        const err = s.error || ri.error || '';
        msg.textContent = '重建失败' + (err ? ': ' + err : '');
        msg.style.color = 'var(--bad)';
        loadIndexInfo();
      }
      return;
    }
    const pct = ri.total ? Math.round(ri.done / ri.total * 100) : 0;
    bar.firstElementChild.style.width = pct + '%';
    msg.textContent = `构建中 ${ri.done}/${ri.total}（${pct}%）`;
  }
}

/* ---------- 概览 ---------- */
async function loadOverview() {
  try {
    loadIndexInfo();
    const [st, lo] = await Promise.all([
      fetch('/api/eval/stats').then(r=>r.json()),
      fetch('/api/eval/history?max_total=6&limit=10').then(r=>r.json())
    ]);
    renderStats(st);
    renderGrade(st.grades, st.total);
    renderTrend(st.trend);
    renderLow(lo.rows);
  } catch(e) {
    $('loading').textContent = '加载失败: ' + e.message;
  }
}

function renderStats(st) {
  const a = st.avg || {};
  const items = [
    ['总评估数', st.total, ''],
    ['平均总分', a.total, '/10'],
    ['准确性', a.accuracy, '/10'],
    ['相关性', a.relevance, '/10'],
    ['完整性', a.completeness, '/10'],
    ['来源支撑', a.source_support, '/10']
  ];
  $('stats').innerHTML = items.map(([k,v,s]) =>
    `<div class="card stat"><div class="v" style="color:${v==null?'var(--muted)':cls(v)}">${
      v==null?'-':v}</div><div class="k">${k}</div><div class="sub">${s}</div></div>`
  ).join('');
}

function renderGrade(g, total) {
  if (!total) { $('legend').textContent = '(暂无数据)'; return; }
  const goodP = (g.good/total*100).toFixed(1);
  const midP = (g.mid/total*100).toFixed(1);
  const badP = (g.bad/total*100).toFixed(1);
  $('gGood').style.width = goodP+'%';
  $('gMid').style.width = midP+'%';
  $('gBad').style.width = badP+'%';
  $('legend').innerHTML = `
    <span><span class="dot" style="background:var(--good)"></span>优秀 ≥8 · ${g.good} 题（${goodP}%）</span>
    <span><span class="dot" style="background:var(--mid)"></span>中等 6-8 · ${g.mid} 题（${midP}%）</span>
    <span><span class="dot" style="background:var(--bad)"></span>待改进 &lt;6 · ${g.bad} 题（${badP}%）</span>`;
}

function renderTrend(trend) {
  const el = $('chart');
  if (!trend || trend.length < 1) { el.innerHTML = '<div class="empty">暂无趋势数据</div>'; return; }
  const W=900, H=150, padL=34, padB=22, padT=8;
  const data = trend.filter(t=>t.avg_total!=null);
  if (!data.length) { el.innerHTML = '<div class="empty">暂无趋势数据</div>'; return; }
  const max = Math.max(10, ...data.map(d=>d.avg_total));
  const x = i => padL + i*(W-padL)/(data.length-1 || 1);
  const y = v => H-padB - (v-0)/(max)* (H-padB-padT);
  const pts = data.map((d,i)=>`${x(i).toFixed(1)},${y(d.avg_total).toFixed(1)}`).join(' ');
  const area = `${padL},${H-padB} ${pts} ${x(data.length-1)},${H-padB}`;
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
  [5,10].forEach(g=>{
    svg += `<line x1="${padL}" y1="${y(g)}" x2="${W}" y2="${y(g)}" stroke="#eef1f5" stroke-dasharray="3 3"/>`;
    svg += `<text class="yl" x="4" y="${y(g)+3}">${g}</text>`;
  });
  svg += `<polygon points="${area}" fill="rgba(37,99,235,.08)"/>`;
  svg += `<polyline points="${pts}" fill="none" stroke="#2563eb" stroke-width="2.5"/>`;
  data.forEach((d,i)=>{
    if (data.length<=12) svg += `<circle class="pt" cx="${x(i)}" cy="${y(d.avg_total)}" r="3.5"/>`;
  });
  if (data.length<=8) data.forEach((d,i)=>{
    svg += `<text class="xl" x="${x(i)}" y="${H-4}">${d.day.slice(5)}</text>`;
  });
  svg += '</svg>';
  el.innerHTML = svg;
}

function renderLow(rows) {
  const el = $('low');
  if (!rows.length) { el.innerHTML = '<div class="empty">暂无待改进问题 🎉</div>'; return; }
  el.innerHTML = rows.map(r=>{
    const w = r.weaknesses || [];
    return `<div class="low-item">
      <div><div style="font-weight:500">${esc(r.question)}</div>
        <div class="mini">${esc(w[0]||r.reason||'')}</div></div>
      <div style="text-align:right">${tag(r.total)}<div class="mini">${r.ts}</div></div>
    </div>`;
  }).join('');
}

/* ---------- 访问记录 ---------- */
const VS = { offset:0, kind:'', kw:'', size:50 };
document.querySelectorAll('#tab-visits .chip').forEach(c => c.addEventListener('click', () => {
  document.querySelectorAll('#tab-visits .chip').forEach(x => x.classList.toggle('on', x === c));
  VS.kind = c.dataset.k; loadVisits(true);
}));
$('visitKw').addEventListener('keydown', e => { if (e.key === 'Enter') loadVisits(true); });

async function loadVisitStats() {
  try {
    const s = await fetch('/api/visit/stats').then(r=>r.json());
    const items = [
      ['总访问', s.total, ''],
      ['今日访问', s.today, ''],
      ['问答查询', (s.by_kind||{}).query, ''],
      ['页面访问', (s.by_kind||{}).page, ''],
      ['平均耗时', s.avg_latency_ms==null?'-':(s.avg_latency_ms/1000).toFixed(1), '秒'],
      ['异常次数', s.errors, s.errors ? '需关注' : '正常']
    ];
    $('visitStats').innerHTML = items.map(([k,v,d]) =>
      `<div class="card stat"><div class="v" style="color:${
        k==='异常次数' && v ? 'var(--bad)' : 'var(--text)'}">${v==null?'-':v}</div>
       <div class="k">${k}</div><div class="d">${d}</div></div>`).join('');
  } catch(e) {
    $('visitStats').innerHTML = '<div class="empty">访问统计加载失败: ' + esc(e.message) + '</div>';
  }
}

async function loadVisits(reset) {
  if (reset) VS.offset = 0;
  VS.kw = $('visitKw').value.trim();
  const url = `/api/visits?limit=${VS.size}&offset=${VS.offset}` +
              (VS.kind ? `&kind=${VS.kind}` : '') + (VS.kw ? `&q=${encodeURIComponent(VS.kw)}` : '');
  try {
    const d = await fetch(url).then(r=>r.json());
    const rows = d.rows || [];
    const html = rows.map(r => {
      const isPage = r.kind === 'page';
      const content = isPage ? (r.path || '-') : (r.question || '-');
      return `<tr>
        <td class="ts">${r.ts}</td>
        <td><span class="tag ${isPage?'tg-page':'tg-info'}">${isPage?'页面':'问答'}</span></td>
        <td class="q" title="${esc(content)}">${esc(content)}</td>
        <td class="mini">${esc(r.endpoint||'-')}</td>
        <td>${r.n_sources ?? '-'}</td>
        <td>${r.n_steps ?? '-'}</td>
        <td class="mini">${fmtMs(r.latency_ms)}</td>
        <td>${r.status === 'ok' ? '<span class="tag tg-good">正常</span>'
             : `<span class="tag tg-err">${esc(r.status||'异常')}</span>`}</td>
      </tr>`;
    }).join('');
    const body = $('visitRows');
    if (reset || VS.offset === 0) body.innerHTML = rows.length ? html
      : '<tr><td colspan="8" class="empty">暂无访问记录</td></tr>';
    else body.insertAdjacentHTML('beforeend', html || '');
    VS.offset += rows.length;
    $('visitMore').style.display = rows.length === VS.size ? '' : 'none';
    loadVisitStats();
  } catch(e) {
    if (reset) $('visitRows').innerHTML = `<tr><td colspan="8" class="empty">加载失败: ${esc(e.message)}</td></tr>`;
  }
}

/* ---------- 评估记录 ---------- */
const ES = { offset:0, band:'', kw:'', size:50 };
document.querySelectorAll('#tab-evals .chip').forEach(c => c.addEventListener('click', () => {
  document.querySelectorAll('#tab-evals .chip').forEach(x => x.classList.toggle('on', x === c));
  ES.band = c.dataset.s; loadEvals(true);
}));
$('evalKw').addEventListener('keydown', e => { if (e.key === 'Enter') loadEvals(true); });

function bandRange(b) {
  if (b === 'bad') return [null, 6];
  if (b === 'mid') return [6, 8];
  if (b === 'good') return [8, null];
  return [null, null];
}

async function loadEvalSummary() {
  try {
    const s = await fetch('/api/eval/stats').then(r=>r.json());
    const a = s.avg || {}, g = s.grades || {};
    const items = [
      ['总评估数', s.total, ''], ['平均总分', a.total==null?'-':a.total, '/10'],
      ['优秀 ≥8', g.good, ''], ['中等 6-8', g.mid, ''], ['待改进 <6', g.bad, ''],
      ['来源支撑', a.source_support==null?'-':a.source_support, '/10']
    ];
    $('evalSummary').innerHTML = items.map(([k,v,d]) =>
      `<div class="card stat"><div class="v" style="color:${typeof v==='number'?cls(v):'var(--muted)'}">${
        v==null?'-':v}</div><div class="k">${k}</div><div class="d">${d}</div></div>`).join('');
  } catch(e) {
    $('evalSummary').innerHTML = '<div class="empty">评估统计加载失败: ' + esc(e.message) + '</div>';
  }
}

function evalRow(r, i) {
  const w = (r.weaknesses || [])[0] || r.reason || '';
  return `<tr class="click" data-i="${i}" onclick="toggleDetail(${i})">
    <td class="ts">${r.ts}</td>
    <td class="q" title="${esc(r.question)}">${esc(r.question)}</td>
    <td class="sc ${r.total==null?'':cls(r.total)}">${r.total==null?'-':r.total.toFixed(1)}</td>
    <td>${r.accuracy??'-'}</td><td>${r.relevance??'-'}</td><td>${r.completeness??'-'}</td>
    <td>${r.clarity??'-'}</td><td>${r.source_support??'-'}</td>
    <td class="mini">${fmtMs(r.latency_ms)}</td>
    <td class="q" title="${esc(w)}">${esc(w)}</td>
  </tr>
  <tr class="detail" id="d${i}" hidden><td colspan="10">
    <div class="detail-grid">
      <div class="detail-box">
        <h4>回答（截断至 4000 字）</h4>
        <div class="body">${esc(r.answer || '(无)')}</div>
      </div>
      <div class="detail-box">
        <h4>评分理由</h4>
        <div class="body" style="max-height:120px">${esc(r.reason || '-')}</div>
        <h4 style="margin-top:10px">优点 / 不足 / 建议</h4>
        <ul>
          ${(r.strengths||[]).map(s=>`<li style="color:var(--good)">✅ ${esc(s)}</li>`).join('')}
          ${(r.weaknesses||[]).map(s=>`<li style="color:var(--mid)">⚠️ ${esc(s)}</li>`).join('')}
          ${r.suggestion?`<li>💡 ${esc(r.suggestion)}</li>`:''}
        </ul>
        <div class="mini" style="margin-top:8px">端口 ${r.port ?? '-'} · 记录 #${r.id}</div>
      </div>
    </div>
  </td></tr>`;
}

function toggleDetail(i) {
  const d = $('d' + i);
  if (d) d.hidden = !d.hidden;
}

async function loadEvals(reset) {
  if (reset) ES.offset = 0;
  ES.kw = $('evalKw').value.trim();
  const [mn, mx] = bandRange(ES.band);
  let url = `/api/eval/history?limit=${ES.size}&offset=${ES.offset}`;
  if (mn != null) url += `&min_total=${mn}`;
  if (mx != null) url += `&max_total=${mx}`;
  if (ES.kw) url += `&q=${encodeURIComponent(ES.kw)}`;
  try {
    const d = await fetch(url).then(r=>r.json());
    const rows = d.rows || [];
    const start = ES.offset;
    const html = rows.map((r, i) => evalRow(r, start + i)).join('');
    const body = $('evalRows');
    if (reset || ES.offset === 0) body.innerHTML = rows.length ? html
      : '<tr><td colspan="10" class="empty">暂无评估记录</td></tr>';
    else body.insertAdjacentHTML('beforeend', html || '');
    ES.offset += rows.length;
    $('evalMore').style.display = rows.length === ES.size ? '' : 'none';
    loadEvalSummary();
  } catch(e) {
    if (reset) $('evalRows').innerHTML = `<tr><td colspan="10" class="empty">加载失败: ${esc(e.message)}</td></tr>`;
  }
}

/* ---------- 初始化 ---------- */
(async function init() {
  loaded.overview = true;
  await loadOverview();
  $('loading').style.display = 'none';
  $('tab-overview').hidden = false;
  try { await fetch('/api/track', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({path: location.pathname, title: document.title})}); } catch {}
})();

const $ = id => document.getElementById(id);
const q = $('q'), btn = $('btn'), status = $('status');

const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const bold = s => esc(s).replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>');
const grade = n => n >= 8 ? ['good','优秀'] : (n >= 6 ? ['mid','中等'] : ['bad','待改进']);
const colorOf = n => n >= 8 ? '#16a34a' : (n >= 6 ? '#d97706' : '#dc2626');

async function ask() {
  const text = q.value.trim();
  if (!text) return;
  btn.disabled = true; status.textContent = '问答 + 多维度评估中…（可能需要 30 秒）';
  $('result').classList.add('hidden');
  try {
    const r = await fetch('/ask_eval', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question:text})
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP '+r.status));
    render(d);
    status.textContent = '完成 · 用时 ' + ((performance.now()-t0)/1000).toFixed(1) + 's';
  } catch(e) {
    status.textContent = '错误: ' + e.message; status.className = 'status';
  } finally { btn.disabled = false; }
}

function render(d) {
  const ev = d.evaluation || {};
  const total = ev.total ?? 0;
  $('result').classList.remove('hidden');
  // 总分环
  $('ring').style.setProperty('--p', total*10);
  $('ring').style.setProperty('--bar', colorOf(total));
  $('totalNum').textContent = total.toFixed(1);
  $('totalNum').style.color = colorOf(total);
  // 标签
  const [cls,label] = grade(total);
  $('tags').innerHTML = `<span class="badge-tag tag-${cls}">${label} · ${total.toFixed(1)}分</span>`;
  $('reason').textContent = ev.reason || '';
  // 维度条
  const dims = ev.dimensions || {};
  const order = ['accuracy','relevance','completeness','clarity','sources'];
  $('dims').innerHTML = order.map(k => {
    const dim = dims[k];
    if (!dim) return '';
    const c = colorOf(dim.score);
    const weight = dim.weight>0 ? ` · 权重 ${Math.round(dim.weight*100)}%` : ' · 辅助';
    return `<div class="dim">
      <div class="head"><span class="name">${esc(dim.name)}<span class="desc">${weight}</span></span>
        <span class="sc" style="color:${c}">${dim.score}/10</span></div>
      <div class="bar"><i style="width:${dim.score*10}%;background:${c}"></i></div>
      <div class="cmt">${esc(dim.comment)}</div>
    </div>`;
  }).join('');
  // 优点/缺点/建议
  const w = [];
  (ev.strengths||[]).forEach(s => w.push(`<li style="color:var(--good)">✅ ${esc(s)}</li>`));
  (ev.weaknesses||[]).forEach(s => w.push(`<li style="color:var(--mid)">⚠️ ${esc(s)}</li>`));
  if (ev.suggestion) w.push(`<li>💡 ${esc(ev.suggestion)}</li>`);
  $('weak').innerHTML = w.length ? '<ul>'+w.join('')+'</ul>' : '(无)';
  // 回答与来源
  $('answer').innerHTML = bold(d.answer || '(空)');
  const srcs = d.sources || [];
  $('srcs').innerHTML = srcs.length
    ? srcs.map(s => `<div class="src">📄 ${esc(s.title)} — <span style="color:var(--muted)">${esc(s.path)}</span></div>`).join('')
    : '(无引用来源)';
}

let t0;
btn.addEventListener('click', () => { t0 = performance.now(); ask(); });
q.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); t0 = performance.now(); ask(); }
});
try { fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify({ path: location.pathname, title: document.title }) }); } catch {}

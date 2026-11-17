const $ = id => document.getElementById(id);
const q = $('q'), btn = $('btn'), status = $('status');

async function health() {
  try {
    const r = await fetch('/health');
    const d = await r.json();
    $('health').textContent = d.status === 'ok' ? '健康' : '异常';
    $('health').className = d.status === 'ok' ? 'ok' : 'error';
    $('chunks').textContent = d.n_chunks ? `· ${d.n_chunks} 个知识块` : '';
  } catch { $('health').textContent = '离线'; $('health').className = 'error'; }
}

const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
// 行内：加粗/斜体/代码/来源引用([来源: X | summary] 或 [[来源: X | summary]])
const inline = s => esc(s)
  .replace(/`([^`]+)`/g, '<code>$1</code>')
  .replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>')
  .replace(/\\[\\[?([^\\]|]+)\\|([^\\]|]+)\\]\\]?/g, (m,a,b)=>`<span class="cite">${a.trim()} · ${b.trim()}</span>`)
  .replace(/\\*([^*]+)\\*/g, '<i>$1</i>');
// 轻量 Markdown 渲染：标题/列表/表格/代码块/引用/水平线/段落
const isRow = t => t.startsWith('|') && t.endsWith('|');
const splitRow = t => {
  // 保护 [来源: X | Y] 引用内的管道符，避免误拆列
  const protected_ = [];
  t = t.replace(/\\[\\[?[^\\]]*?\\|[^\\]]*?\\]\\]?/g, m => { protected_.push(m); return '\\x01' + (protected_.length-1) + '\\x02'; });
  const cells = t.replace(/^\\||\\|$/g, '').split('|').map(x => x.trim());
  return cells.map(c => c.replace(/\\x01(\\d+)\\x02/g, (_,i) => protected_[+i]));
}
function render(md) {
  const lines = (md||'').split('\\n');
  const out = [];
  let list = null, code = null;
  const flushList = () => {
    if (list) { out.push(`<${list.type}>` + list.items.map(x=>`<li>${x}</li>`).join('') + `</${list.type}>`); list = null; }
  };
  let i = 0;
  while (i < lines.length) {
    const raw = lines[i];
    const t = raw.trim();
    if (t.startsWith('```')) {
      if (code !== null) { out.push(`<pre><code>${esc(code.join('\\n'))}</code></pre>`); code = null; }
      else code = [];
      i++; continue;
    }
    if (code) { code.push(raw); i++; continue; }
    if (!t) { flushList(); i++; continue; }
    // 表格：| a | b | 开头，且下一行是分隔线 |---|---|
    const isSep = i + 1 < lines.length && isRow(lines[i+1].trim())
                  && splitRow(lines[i+1].trim()).every(c => /^:?-+:?$/.test(c));
    if (isRow(t) && isSep) {
      flushList();
      const head = splitRow(t).map(x => `<th>${inline(x)}</th>`).join('');
      let body = '';
      i += 2; // 跳过表头和数据分隔线
      while (i < lines.length && isRow(lines[i].trim())) {
        body += `<tr>` + splitRow(lines[i].trim()).map(x => `<td>${inline(x)}</td>`).join('') + `</tr>`;
        i++;
      }
      out.push(`<div class="twrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
      continue;
    }
    const h = t.match(/^(#{1,4})\\s+(.*)/);
    if (h) { flushList(); const n = Math.min(h[1].length, 4); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
    if (/^-{3,}$/.test(t)) { flushList(); out.push('<hr>'); i++; continue; }
    const ol = t.match(/^\\d+\\.\\s+(.*)/);
    const ul = t.match(/^[-*]\\s+(.*)/);
    if (ol || ul) {
      const type = ol ? 'ol' : 'ul';
      if (!list || list.type !== type) flushList();
      if (!list) list = { type, items: [] };
      list.items.push(inline((ol ? ol[1] : ul[1])));
      i++; continue;
    }
    if (t.startsWith('> ')) { flushList(); out.push(`<blockquote>${inline(t.slice(2))}</blockquote>`); i++; continue; }
    flushList();
    out.push(`<p>${inline(t)}</p>`);
    i++;
  }
  if (code) out.push(`<pre><code>${esc(code.join('\\n'))}</code></pre>`);
  flushList();
  return out.join('\\n');
}

async function ask() {
  const text = q.value.trim();
  if (!text) return;
  btn.disabled = true;
  status.className = 'status';
  status.textContent = '思考中…';
  $('ansCard').hidden = true; $('srcCard').hidden = true; $('stepsCard').hidden = true;
  try {
    const r = await fetch('/ask_smart', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text, stateless: true })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    $('answer').innerHTML = render(d.answer || '(空回答)');
    $('ansCard').hidden = false;
    renderSteps(d.steps);
    const srcs = d.sources || [];
    if (srcs.length) {
      $('srcs').innerHTML = srcs.map(s =>
        `<div class="src"><span class="t">${esc(s.title)}</span>` +
        `<span class="p">${esc(s.path)}</span></div>`).join('');
      $('srcCard').hidden = false;
    }
    status.textContent = `完成 · 用时 ${((performance.now() - t0)/1000).toFixed(1)}s`;
  } catch (e) {
    status.textContent = '错误: ' + e.message;
    status.className = 'status error';
  } finally { btn.disabled = false; }
}

// 逐步执行结果：每一步展开后按「输入 → 待处理 → 处理后结果 → 输出 →
// 详细操作 → 关键明细 → 命中条目」分区展示
const STATUS_TEXT = { ok: '成功', empty: '无结果', skip: '已跳过', error: '失败' };
const IO_TITLES = { input: '输入', pending: '待处理信息',
                    result: '处理后结果', output: '输出' };
let stepsExpanded = false;

function ioHtml(io) {
  if (!io) return '';
  return Object.keys(IO_TITLES).map(k => {
    const rows = io[k] || [];
    if (!rows.length) return '';
    const body = rows.map(e => {
      const v = String(e.value == null ? '' : e.value);
      const long = v.length > 160 || v.includes('\\n');
      return `<div class="io-row">
        <div class="io-l">${esc(e.label)}${e.meta ? `<span class="io-m">${esc(e.meta)}</span>` : ''}</div>
        ${long ? `<pre class="io-pre">${esc(v)}</pre>` : `<div class="io-v">${esc(v)}</div>`}
      </div>`;
    }).join('');
    return `<div class="st-sec">${IO_TITLES[k]}</div><div class="io-list">${body}</div>`;
  }).join('');
}

function renderSteps(steps) {
  const card = $('stepsCard'), box = $('steps');
  if (!steps || !steps.length) { card.hidden = true; return; }
  const timed = steps.filter(s => s.ms != null).reduce((a, s) => a + s.ms, 0);
  $('stepsMeta').textContent = `${steps.length} 步 · 计时合计 ${(timed/1000).toFixed(1)}s`;
  box.innerHTML = steps.map((s, i) => {
    const ops = (s.ops || []).map(o => `<li>${esc(o)}</li>`).join('');
    const io = ioHtml(s.io);
    const items = (s.items || []).map(it => `<div class="item">
        <div style="min-width:0; flex:1">
          <div class="t">${esc(it.title)}</div>
          <div class="p">${esc(it.path)}${it.type ? ' · ' + esc(it.type) : ''}</div>
          ${it.snippet ? `<div class="sn">${esc(it.snippet)}</div>` : ''}
        </div>
        <div class="s">${it.score != null ? Number(it.score).toFixed(3) : ''}</div>
      </div>`).join('');
    const st = STATUS_TEXT[s.status] || s.status || '';
    return `<details class="step"${stepsExpanded ? ' open' : ''}>
      <summary>
        <span class="st-no">${i + 1}</span>
        <span class="st-ic">${s.icon || '•'}</span>
        <span class="st-title">${esc(s.title)}</span>
        <span class="st-sum">${esc(s.summary || '')}</span>
        <span class="st-ms">${st}${s.ms != null ? ' · ' + s.ms + 'ms' : ''}</span>
      </summary>
      <div class="st-body">
        ${io}
        ${ops ? `<div class="st-sec">详细操作</div><ol class="ops">${ops}</ol>` : ''}
        ${s.detail ? `<div class="st-sec">关键明细</div><div class="st-detail">${esc(s.detail)}</div>` : ''}
        ${items ? `<div class="st-sec">结果条目（${(s.items || []).length} 条）</div>${items}`
                : (io || ops || s.detail ? '' : '<div class="st-detail">本步无明细</div>')}
      </div>
    </details>`;
  }).join('');
  card.hidden = false;
}

$('stepsToggle').addEventListener('click', () => {
  stepsExpanded = !stepsExpanded;
  document.querySelectorAll('#steps .step').forEach(d => { d.open = stepsExpanded; });
  $('stepsToggle').textContent = stepsExpanded ? '全部收起' : '全部展开';
});

let t0;
btn.addEventListener('click', () => { t0 = performance.now(); ask(); });
q.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); t0 = performance.now(); ask(); }
});
health();
try { fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' },
       body: JSON.stringify({ path: location.pathname, title: document.title }) }); } catch {}

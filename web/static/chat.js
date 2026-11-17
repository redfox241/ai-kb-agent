const $ = id => document.getElementById(id);
const msgs = $('msgs'), q = $('q'), sendBtn = $('sendBtn');
/* 会话身份：localStorage 优先，cookie 兜底（服务端也会下发同名 cookie） */
const COOKIE_NAME = 'kb_session_' + (location.port || '80');
function readCookie() {
  const m = document.cookie.match(new RegExp('(?:^|; )' + COOKIE_NAME + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : null;
}
function clearSessionCookie() {
  document.cookie = COOKIE_NAME + '=; Max-Age=0; path=/';
}
const state = {
  sessionId: localStorage.getItem('kb_session') || readCookie() || null,
  busy: false
};

/* ---------- Markdown 渲染（与 ui.py 保持一致） ---------- */
const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const inline = s => esc(s)
  .replace(/`([^`]+)`/g, '<code>$1</code>')
  .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
  .replace(/\[\[?([^\]|]+)\|([^\]|]+)\]\]?/g, (m,a,b)=>`<span class="cite">${a.trim()} · ${b.trim()}</span>`)
  .replace(/\*([^*]+)\*/g, '<i>$1</i>');
const isRow = t => t.startsWith('|') && t.endsWith('|');
const splitRow = t => {
  const keep = [];
  t = t.replace(/\[\[?[^\]]*?\|[^\]]*?\]\]?/g, m => { keep.push(m); return '\x01' + (keep.length-1) + '\x02'; });
  const cells = t.replace(/^\||\|$/g, '').split('|').map(x => x.trim());
  return cells.map(c => c.replace(/\x01(\d+)\x02/g, (_,i) => keep[+i]));
};
function render(md) {
  const lines = (md||'').split('\n');
  const out = [];
  let list = null, code = null;
  const flushList = () => {
    if (list) { out.push(`<${list.type}>` + list.items.map(x=>`<li>${x}</li>`).join('') + `</${list.type}>`); list = null; }
  };
  let i = 0;
  while (i < lines.length) {
    const raw = lines[i], t = raw.trim();
    if (t.startsWith('```')) {
      if (code !== null) { out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); code = null; }
      else code = [];
      i++; continue;
    }
    if (code) { code.push(raw); i++; continue; }
    if (!t) { flushList(); i++; continue; }
    const isSep = i + 1 < lines.length && isRow(lines[i+1].trim())
                  && splitRow(lines[i+1].trim()).every(c => /^:?-+:?$/.test(c));
    if (isRow(t) && isSep) {
      flushList();
      const head = splitRow(t).map(x => `<th>${inline(x)}</th>`).join('');
      let body = '';
      i += 2;
      while (i < lines.length && isRow(lines[i].trim())) {
        body += `<tr>` + splitRow(lines[i].trim()).map(x => `<td>${inline(x)}</td>`).join('') + `</tr>`;
        i++;
      }
      out.push(`<div class="twrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
      continue;
    }
    const h = t.match(/^(#{1,4})\s+(.*)/);
    if (h) { flushList(); const n = Math.min(h[1].length, 4); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
    if (/^-{3,}$/.test(t)) { flushList(); out.push('<hr>'); i++; continue; }
    const ol = t.match(/^\d+\.\s+(.*)/), ul = t.match(/^[-*]\s+(.*)/);
    if (ol || ul) {
      const type = ol ? 'ol' : 'ul';
      if (!list || list.type !== type) flushList();
      if (!list) list = { type, items: [] };
      list.items.push(inline(ol ? ol[1] : ul[1]));
      i++; continue;
    }
    if (t.startsWith('> ')) { flushList(); out.push(`<blockquote>${inline(t.slice(2))}</blockquote>`); i++; continue; }
    flushList();
    out.push(`<p>${inline(t)}</p>`);
    i++;
  }
  if (code) out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  flushList();
  return out.join('\n');
}

/* ---------- 轨迹渲染（每轮独立，互不干扰） ---------- */
const IO_TITLES = { input: '输入', pending: '待处理信息', result: '处理后结果', output: '输出' };
const STATUS_TEXT = { ok: '成功', empty: '无结果', skip: '已跳过', error: '失败' };

function ioHtml(io) {
  if (!io) return '';
  return Object.keys(IO_TITLES).map(k => {
    const rows = io[k] || [];
    if (!rows.length) return '';
    const body = rows.map(e => {
      const v = String(e.value == null ? '' : e.value);
      const long = v.length > 160 || v.includes('\n');
      return `<div class="io-row"><div class="io-l">${esc(e.label)}${e.meta ? `<span class="io-m">${esc(e.meta)}</span>` : ''}</div>
        ${long ? `<pre class="io-pre">${esc(v)}</pre>` : `<div class="io-v">${esc(v)}</div>`}</div>`;
    }).join('');
    return `<div class="st-sec">${IO_TITLES[k]}</div>${body}`;
  }).join('');
}

function stepsHtml(steps) {
  if (!steps || !steps.length) return '';
  const timed = steps.filter(s => s.ms != null).reduce((a, s) => a + s.ms, 0);
  const items = steps.map((s, i) => {
    const ops = (s.ops || []).map(o => `<li>${esc(o)}</li>`).join('');
    const io = ioHtml(s.io);
    const hits = (s.items || []).map(it => `<div class="item"><div style="min-width:0;flex:1">
        <div class="t">${esc(it.title)}</div>
        <div class="p">${esc(it.path)}${it.type ? ' · ' + esc(it.type) : ''}</div>
        ${it.snippet ? `<div class="sn">${esc(it.snippet)}</div>` : ''}</div>
        <div class="s">${it.score != null ? Number(it.score).toFixed(3) : ''}</div></div>`).join('');
    const st = STATUS_TEXT[s.status] || s.status || '';
    return `<details class="step"><summary>
        <span class="st-no">${i + 1}</span><span>${s.icon || '•'}</span>
        <span class="st-title">${esc(s.title)}</span>
        <span class="st-sum">${esc(s.summary || '')}</span>
        <span class="st-ms">${st}${s.ms != null ? ' · ' + s.ms + 'ms' : ''}</span>
      </summary><div class="sb">${io}
        ${ops ? `<div class="st-sec">详细操作</div><ol class="ops">${ops}</ol>` : ''}
        ${s.detail ? `<div class="st-sec">关键明细</div><div class="st-detail">${esc(s.detail)}</div>` : ''}
        ${hits ? `<div class="st-sec">结果条目（${(s.items || []).length} 条）</div>${hits}` : ''}
      </div></details>`;
  }).join('');
  return `<details class="steps"><summary>执行步骤 · ${steps.length} 步 · 计时 ${(timed/1000).toFixed(1)}s</summary>${items}</details>`;
}

function sourcesHtml(list) {
  const srcs = (list || []).map(s => ({ title: s.title || s.id || '', path: s.path || s.doc_path || '' }));
  if (!srcs.length) return '';
  return `<div class="srcs"><div class="st-sec">参考来源</div>` +
    srcs.map(s => `<div class="src"><span class="t">${esc(s.title)}</span><span class="p">${esc(s.path)}</span></div>`).join('') +
    `</div>`;
}

/* ---------- 消息流 ---------- */
function scrollDown() { const c = $('chat'); c.scrollTop = c.scrollHeight; }
function hideEmpty() { const e = $('empty'); if (e) e.remove(); }

function addUser(text) {
  hideEmpty();
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `<div class="avatar">你</div><div class="bubble">${render(text)}</div>`;
  msgs.appendChild(el); scrollDown();
  return el;
}

function addAssistant(d, opts) {
  opts = opts || {};
  const el = document.createElement('div');
  el.className = 'msg assistant';
  const meta = [];
  if (d.intent) meta.push(`<span class="intent">${esc(d.intent)}</span>`);
  if (d.resolved_query && opts.question && d.resolved_query !== opts.question) {
    meta.push(`已解析为：${esc(d.resolved_query)}`);
  }
  if (d.citations && d.citations.invalid && d.citations.invalid.length) {
    meta.push(`<span style="color:var(--warn)">引用编号异常：${esc(d.citations.invalid.join(','))}</span>`);
  }
  const chips = (d.followups || []).map(f => `<span class="chip" data-q="${esc(f)}">${esc(f)}</span>`).join('');
  el.innerHTML = `<div class="avatar">AI</div><div class="bubble">
      ${meta.length ? `<div class="meta">${meta.join(' · ')}</div>` : ''}
      <div class="answer">${render(d.answer || '(空回答)')}</div>
      ${opts.showSteps && d.steps ? stepsHtml(d.steps) : ''}
      ${sourcesHtml(d.sources || d.citations)}
      ${chips ? `<div class="chips">${chips}</div>` : ''}
    </div>`;
  msgs.appendChild(el);
  scrollDown();
  return el;
}

function addPending() {
  hideEmpty();
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = `<div class="avatar">AI</div><div class="bubble pending">思考中 <span>●</span><span>●</span><span>●</span></div>`;
  msgs.appendChild(el); scrollDown();
  return el;
}

function addError(msg, retryText) {
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = `<div class="avatar">AI</div><div class="bubble"><div class="err">请求失败：${esc(msg)}</div>
    <div class="chips"><span class="chip" id="retryChip">重试</span></div></div>`;
  msgs.appendChild(el);
  el.querySelector('#retryChip').onclick = () => { el.remove(); if (retryText) send(retryText); };
  scrollDown();
}

/* ---------- 会话 ---------- */
async function refreshSessions() {
  try {
    const r = await fetch('/api/sessions?limit=30');
    const d = await r.json();
    const sel = $('sessionSel');
    sel.innerHTML = '<option value="">（新会话）</option>' + (d.sessions || []).map(s => {
      const title = (s.title || s.session_id).replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
      return `<option value="${s.session_id}">${title} · ${s.turn_count} 轮</option>`;
    }).join('');
    sel.value = state.sessionId || '';
  } catch (e) { /* 会话列表失败不影响提问 */ }
}

async function loadSession(id) {
  if (!id) return newSession();
  state.sessionId = id; localStorage.setItem('kb_session', id);
  try {
    const r = await fetch('/api/sessions/' + encodeURIComponent(id));
    if (!r.ok) return newSession();
    const d = await r.json();
    msgs.innerHTML = '';
    for (const m of d.messages) {
      if (m.role === 'user') addUser(m.content);
      else addAssistant({ answer: m.content, citations: m.citations,
                          resolved_query: m.resolved_query, intent: m.intent },
                        { showSteps: false });
    }
    if (!d.messages.length) showEmpty();
    scrollDown();
  } catch (e) { newSession(); }
}

function showEmpty() {
  msgs.innerHTML = `<div class="empty" id="empty">
    <div><b>支持多轮追问</b>：上下文会自动关联</div>
    <div>试试：先问「长征从什么时候开始」，再问「它持续了多久？」</div></div>`;
}

function newSession() {
  state.sessionId = null;
  localStorage.removeItem('kb_session');
  clearSessionCookie();
  $('sessionSel').value = '';
  msgs.innerHTML = ''; showEmpty(); q.focus();
}

/* ---------- 发送 ---------- */
async function send(text) {
  const question = (text || q.value).trim();
  if (!question || state.busy) return;
  state.busy = true; sendBtn.disabled = true;
  q.value = ''; q.style.height = 'auto';
  addUser(question);
  const pending = addPending();
  const t0 = performance.now();
  try {
    const r = await fetch('/ask_smart', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, session_id: state.sessionId })
    });
    const d = await r.json();
    if (!r.ok) throw new Error((d.detail && (d.detail.error || d.detail)) || ('HTTP ' + r.status));
    pending.remove();
    if (d.session_id) {
      state.sessionId = d.session_id;
      localStorage.setItem('kb_session', d.session_id);
      refreshSessions();
    }
    addAssistant(d, { showSteps: true, question });
    $('hint').textContent = `完成 · ${((performance.now() - t0)/1000).toFixed(1)}s` +
      (d.memory && d.memory.prompt_tokens ? ` · prompt ${d.memory.prompt_tokens} tokens` : '');
  } catch (e) {
    pending.remove();
    addError(e.message, question);
  } finally {
    state.busy = false; sendBtn.disabled = false; q.focus();
  }
}

/* ---------- 事件 ---------- */
sendBtn.onclick = () => send();
q.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
q.addEventListener('input', () => {
  q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 180) + 'px';
});
msgs.addEventListener('click', e => {
  const chip = e.target.closest('.chip[data-q]');
  if (chip) { send(chip.dataset.q); }
});
$('sessionSel').onchange = e => loadSession(e.target.value);
$('newBtn').onclick = () => newSession();
$('exportBtn').onclick = () => {
  if (state.sessionId) window.open('/api/sessions/' + encodeURIComponent(state.sessionId) + '/export', '_blank');
  else $('hint').textContent = '当前还没有会话可导出';
};
$('delBtn').onclick = async () => {
  if (!state.sessionId) return newSession();
  if (!confirm('删除当前会话？该操作不可恢复。')) return;
  await fetch('/api/sessions/' + encodeURIComponent(state.sessionId), { method: 'DELETE' });
  newSession(); refreshSessions();
};

/* ---------- 启动 ---------- */
(async () => {
  try {
    const r = await fetch('/health'); const d = await r.json();
    $('health').textContent = d.status === 'ok' ? '健康' : '异常';
    $('health').className = d.status === 'ok' ? 'ok' : 'error';
    $('chunks').textContent = d.n_chunks ? `· ${d.n_chunks} 知识块` : '';
  } catch { $('health').textContent = '离线'; $('health').className = 'error'; }
  await refreshSessions();
  if (state.sessionId) await loadSession(state.sessionId);
})();

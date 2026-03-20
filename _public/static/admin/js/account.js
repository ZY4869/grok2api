let apiKey = '';
let allTokens = [];
let pendingConfirmFn = null;

function byId(id) { return document.getElementById(id); }

function escapeHtml(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

function formatTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function init() {
  apiKey = await ensureAdminKey();
  if (apiKey === null) return;
  await loadAccountData();
}

async function loadAccountData() {
  try {
    const res = await fetch('/v1/admin/tokens', { headers: buildAuthHeaders(apiKey) });
    if (!res.ok) throw new Error('Failed to load tokens');
    const data = await res.json();
    const tokens = data.tokens || {};

    allTokens = [];
    Object.entries(tokens).forEach(([pool, list]) => {
      if (!Array.isArray(list)) return;
      list.forEach(t => {
        allTokens.push({
          token: t.token || '',
          pool: pool,
          status: t.status || 'active',
          alive: t.alive != null ? t.alive : null,
          quota: t.quota || 0,
          tags: t.tags || [],
          last_alive_check_at: t.last_alive_check_at,
          _selected: false,
        });
      });
    });

    renderTable();
  } catch (e) {
    console.error(e);
    showToast('加载数据失败', 'error');
  }
}

// ========== 表格渲染 ==========

function getAliveDisplay(item) {
  if (item.status === 'expired') return '<span class="text-red-600 font-bold" title="失效">&#10007;</span>';
  if (item.status === 'cooling') return '<span class="text-orange-500 font-bold" title="限流">&#9724;</span>';
  if (item.status === 'disabled') return '<span class="text-gray-400" title="已禁用">&#9724;</span>';
  if (item.alive === true) return '<span class="text-green-600 font-bold" title="可用">&#10003;</span>';
  if (item.alive === false) return '<span class="text-red-600 font-bold" title="不可用">&#10007;</span>';
  return '<span class="text-gray-400" title="未检测">-</span>';
}

function renderTable() {
  const tbody = byId('account-table-body');
  const empty = byId('empty-state');

  if (allTokens.length === 0) {
    tbody.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  const fragment = document.createDocumentFragment();
  allTokens.forEach((item, idx) => {
    const tr = document.createElement('tr');
    if (item._selected) tr.classList.add('row-selected');

    // 选择
    const tdCheck = document.createElement('td');
    tdCheck.className = 'text-center';
    tdCheck.innerHTML = `<input type="checkbox" class="checkbox" ${item._selected ? 'checked' : ''} onchange="toggleSelect(${idx})">`;

    // Token
    const tdToken = document.createElement('td');
    tdToken.className = 'text-left';
    const short = item.token.length > 24
      ? item.token.substring(0, 8) + '...' + item.token.substring(item.token.length - 16)
      : item.token;
    tdToken.innerHTML = `<span class="font-mono text-xs text-gray-500" title="${escapeHtml(item.token)}">${escapeHtml(short)}</span>`;

    // 类型
    const tdPool = document.createElement('td');
    tdPool.className = 'text-center';
    tdPool.innerHTML = `<span class="badge badge-gray">${escapeHtml(item.pool)}</span>`;

    // 可用（合并状态）
    const tdAlive = document.createElement('td');
    tdAlive.className = 'text-center text-sm';
    tdAlive.innerHTML = getAliveDisplay(item);

    // NSFW
    const tdNsfw = document.createElement('td');
    tdNsfw.className = 'text-center text-sm';
    const hasNsfw = item.tags && item.tags.includes('nsfw');
    tdNsfw.innerHTML = hasNsfw
      ? '<span class="text-purple-600 font-bold" title="NSFW 已开">&#10003;</span>'
      : '<span class="text-gray-400" title="NSFW 未开">&#10007;</span>';

    // 额度
    const tdQuota = document.createElement('td');
    tdQuota.className = 'text-center font-mono text-xs';
    tdQuota.textContent = item.quota;

    // 上次检测
    const tdLastCheck = document.createElement('td');
    tdLastCheck.className = 'text-center text-xs text-gray-500';
    tdLastCheck.textContent = formatTime(item.last_alive_check_at);

    // 操作
    const tdActions = document.createElement('td');
    tdActions.className = 'text-center';
    const isDisabled = item.status === 'disabled';
    const toggleIcon = isDisabled
      ? '<polyline points="20 6 9 17 4 12"></polyline>'
      : '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>';
    const toggleClass = isDisabled ? 'hover:text-green-600' : 'hover:text-orange-600';
    const toggleTitle = isDisabled ? '启用' : '禁用';
    const hasNsfwTag = item.tags && item.tags.includes('nsfw');
    const nsfwBtnClass = hasNsfwTag ? 'text-purple-500 hover:text-gray-400' : 'text-gray-400 hover:text-purple-500';
    const nsfwTitle = hasNsfwTag ? '关闭 NSFW' : '开启 NSFW';
    tdActions.innerHTML = `
      <div class="flex items-center justify-center gap-1">
        <button onclick="checkSingleAlive('${item.token}')" class="p-1 text-gray-400 hover:text-green-600 rounded" title="检测">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>
        </button>
        <button onclick="toggleSingleNSFW(${idx})" class="p-1 ${nsfwBtnClass} rounded" title="${nsfwTitle}">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
        </button>
        <button onclick="toggleSingleStatus(${idx})" class="p-1 text-gray-400 ${toggleClass} rounded" title="${toggleTitle}">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${toggleIcon}</svg>
        </button>
        <button onclick="deleteSingle('${item.token}', '${item.pool}')" class="p-1 text-gray-400 hover:text-red-600 rounded" title="删除">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      </div>
    `;

    tr.appendChild(tdCheck);
    tr.appendChild(tdToken);
    tr.appendChild(tdPool);
    tr.appendChild(tdAlive);
    tr.appendChild(tdNsfw);
    tr.appendChild(tdQuota);
    tr.appendChild(tdLastCheck);
    tr.appendChild(tdActions);
    fragment.appendChild(tr);
  });

  tbody.replaceChildren(fragment);
}

// ========== 选择功能 ==========

function toggleSelect(idx) {
  allTokens[idx]._selected = !allTokens[idx]._selected;
  renderTable();
}

function toggleSelectAll() {
  const cb = byId('select-all');
  const checked = !!(cb && cb.checked);
  allTokens.forEach(t => t._selected = checked);
  renderTable();
}

function getSelected() {
  return allTokens.filter(t => t._selected);
}

// ========== 批量启用/禁用 ==========

async function updateTokenStatus(tokens, newStatus) {
  try {
    const res = await fetch('/v1/admin/tokens', { headers: buildAuthHeaders(apiKey) });
    const data = await res.json();
    const existing = data.tokens || {};
    const targetSet = new Set(tokens.map(t => t.token));

    for (const [pool, list] of Object.entries(existing)) {
      existing[pool] = list.map(t => {
        if (targetSet.has(t.token)) {
          return { ...t, status: newStatus };
        }
        return t;
      });
    }

    const saveRes = await fetch('/v1/admin/tokens', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
      body: JSON.stringify(existing)
    });
    return saveRes.ok;
  } catch (e) {
    console.error(e);
    return false;
  }
}

async function batchEnable() {
  const selected = getSelected();
  if (!selected.length) { showToast('请先选择账号', 'info'); return; }
  const ok = await updateTokenStatus(selected, 'active');
  if (ok) {
    await loadAccountData();
    showToast(`已启用 ${selected.length} 个账号`, 'success');
  } else {
    showToast('操作失败', 'error');
  }
}

async function batchDisable() {
  const selected = getSelected();
  if (!selected.length) { showToast('请先选择账号', 'info'); return; }
  const ok = await updateTokenStatus(selected, 'disabled');
  if (ok) {
    await loadAccountData();
    showToast(`已禁用 ${selected.length} 个账号`, 'success');
  } else {
    showToast('操作失败', 'error');
  }
}

async function toggleSingleStatus(idx) {
  const item = allTokens[idx];
  const newStatus = item.status === 'disabled' ? 'active' : 'disabled';
  const ok = await updateTokenStatus([item], newStatus);
  if (ok) {
    await loadAccountData();
    showToast(newStatus === 'active' ? '已启用' : '已禁用', 'success');
  } else {
    showToast('操作失败', 'error');
  }
}

// ========== NSFW 功能 ==========

async function enableNSFWForTokens(tokens) {
  try {
    const res = await fetch('/v1/admin/tokens/nsfw/enable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
      body: JSON.stringify({ tokens: tokens })
    });
    const data = await res.json();
    return res.ok && data.status === 'success';
  } catch (e) {
    console.error(e);
    return false;
  }
}

async function toggleSingleNSFW(idx) {
  const item = allTokens[idx];
  const hasNsfw = item.tags && item.tags.includes('nsfw');

  if (hasNsfw) {
    // 关闭 NSFW：从 tags 中移除
    try {
      const res = await fetch('/v1/admin/tokens', { headers: buildAuthHeaders(apiKey) });
      const data = await res.json();
      const existing = data.tokens || {};
      for (const [pool, list] of Object.entries(existing)) {
        existing[pool] = list.map(t => {
          if (t.token === item.token) {
            const tags = (t.tags || []).filter(tag => tag !== 'nsfw');
            return { ...t, tags };
          }
          return t;
        });
      }
      const saveRes = await fetch('/v1/admin/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
        body: JSON.stringify(existing)
      });
      if (saveRes.ok) {
        await loadAccountData();
        showToast('NSFW 已关闭', 'success');
      } else { showToast('操作失败', 'error'); }
    } catch (e) { console.error(e); showToast('请求失败', 'error'); }
  } else {
    // 开启 NSFW：调用 API
    const ok = await enableNSFWForTokens([item.token]);
    if (ok) {
      await loadAccountData();
      showToast('NSFW 已开启', 'success');
    } else {
      showToast('开启 NSFW 失败（需要代理或 cf_clearance）', 'error');
    }
  }
}

async function batchEnableNSFW() {
  const selected = getSelected();
  if (!selected.length) { showToast('请先选择账号', 'info'); return; }
  const tokens = selected.map(t => t.token);
  const ok = await enableNSFWForTokens(tokens);
  if (ok) {
    await loadAccountData();
    showToast(`已为 ${tokens.length} 个账号开启 NSFW`, 'success');
  } else {
    showToast('开启 NSFW 失败（需要代理或 cf_clearance）', 'error');
  }
}

// ========== 检测功能 ==========

async function checkAllAlive() {
  const btn = byId('btn-check-all');
  const progress = byId('check-progress');
  if (!allTokens.length) { showToast('没有账号需要检测', 'info'); return; }

  btn.disabled = true;
  btn.textContent = '检测中...';
  progress.classList.remove('hidden');
  progress.textContent = '正在检测所有账号...';

  try {
    const tokens = allTokens.map(t => t.token);
    const res = await fetch('/v1/admin/tokens/alive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
      body: JSON.stringify({ tokens })
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      const results = data.results || {};
      let ok = 0, fail = 0;
      for (const v of Object.values(results)) {
        if (v === true) ok++; else fail++;
      }
      await loadAccountData();
      showToast(`检测完成: ${ok} 可用, ${fail} 不可用`, 'success');
    } else {
      showToast('检测失败', 'error');
    }
  } catch (e) {
    console.error(e);
    showToast('请求失败', 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg> 全部检测`;
    progress.classList.add('hidden');
  }
}

async function checkSingleAlive(token) {
  try {
    const res = await fetch('/v1/admin/tokens/alive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
      body: JSON.stringify({ token })
    });
    const data = await res.json();
    if (res.ok && data.status === 'success') {
      const alive = data.results && data.results[token];
      await loadAccountData();
      showToast(alive === true ? 'Token 可用' : (alive === false ? 'Token 不可用' : '检测结果未知'), alive === true ? 'success' : 'error');
    } else {
      showToast('检测失败', 'error');
    }
  } catch (e) {
    console.error(e);
    showToast('请求失败', 'error');
  }
}

// ========== 清理/删除 ==========

function cleanExpired() {
  const expired = allTokens.filter(t => t.alive === false || t.status === 'expired');
  if (expired.length === 0) { showToast('没有失效账号需要清理', 'info'); return; }
  showConfirm('清理失效账号', `确认删除 ${expired.length} 个失效账号？`, async () => {
    try {
      const res = await fetch('/v1/admin/tokens', { headers: buildAuthHeaders(apiKey) });
      const data = await res.json();
      const tokens = data.tokens || {};
      const expiredSet = new Set(expired.map(t => t.token));
      const cleaned = {};
      for (const [pool, list] of Object.entries(tokens)) {
        cleaned[pool] = list.filter(t => !expiredSet.has(t.token));
      }
      const saveRes = await fetch('/v1/admin/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
        body: JSON.stringify(cleaned)
      });
      if (saveRes.ok) {
        await loadAccountData();
        showToast(`已清理 ${expired.length} 个失效账号`, 'success');
      } else { showToast('清理失败', 'error'); }
    } catch (e) { console.error(e); showToast('请求失败', 'error'); }
  });
}

async function deleteSingle(token, pool) {
  showConfirm('删除账号', '确认删除此 Token？', async () => {
    try {
      const res = await fetch('/v1/admin/tokens', { headers: buildAuthHeaders(apiKey) });
      const data = await res.json();
      const tokens = data.tokens || {};
      const cleaned = {};
      for (const [p, list] of Object.entries(tokens)) {
        cleaned[p] = list.filter(t => t.token !== token);
      }
      const saveRes = await fetch('/v1/admin/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
        body: JSON.stringify(cleaned)
      });
      if (saveRes.ok) { await loadAccountData(); showToast('已删除', 'success'); }
      else { showToast('删除失败', 'error'); }
    } catch (e) { console.error(e); showToast('请求失败', 'error'); }
  });
}

// ========== 导入功能 ==========

function openImportModal() {
  const modal = byId('import-modal');
  modal.classList.remove('hidden');
  requestAnimationFrame(() => modal.classList.add('is-open'));
  byId('import-text').value = '';
  byId('import-text').placeholder = '粘贴 Token，一行一个...';
  const csvInput = byId('import-csv');
  if (csvInput) csvInput.value = '';
}

function closeImportModal() {
  const modal = byId('import-modal');
  modal.classList.remove('is-open');
  setTimeout(() => modal.classList.add('hidden'), 200);
}

function addToken() {
  openImportModal();
  byId('import-text').placeholder = '输入单个 Token...';
}

function handleCsvUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(e) {
    const text = e.target.result;
    const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
    const tokens = [];
    lines.forEach((line, i) => {
      if (i === 0 && line.toLowerCase().includes('token')) return; // 跳过表头
      const parts = line.split(',');
      const token = (parts[0] || '').trim();
      const pool = (parts[1] || '').trim();
      if (token) {
        if (pool) {
          tokens.push(pool + ':' + token); // pool:token 格式
        } else {
          tokens.push(token);
        }
      }
    });
    byId('import-text').value = tokens.join('\n');
    showToast(`已读取 ${tokens.length} 条记录`, 'success');
  };
  reader.readAsText(file);
}

function downloadTemplate() {
  const csv = 'token,pool\nyour_token_here,ssoBasic\n';
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'token_import_template.csv';
  a.click();
}

async function submitImport() {
  const defaultPool = byId('import-pool').value;
  const text = byId('import-text').value.trim();
  if (!text) { showToast('请输入 Token 或上传 CSV', 'error'); return; }

  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  if (lines.length === 0) { showToast('没有有效的 Token', 'error'); return; }

  try {
    const res = await fetch('/v1/admin/tokens', { headers: buildAuthHeaders(apiKey) });
    const data = await res.json();
    const existing = data.tokens || {};

    let added = 0;
    lines.forEach(line => {
      let pool = defaultPool;
      let token = line;
      // 支持 pool:token 格式（CSV 上传时）
      if (line.includes(':') && (line.startsWith('ssoBasic:') || line.startsWith('ssoSuper:'))) {
        const idx = line.indexOf(':');
        pool = line.substring(0, idx);
        token = line.substring(idx + 1);
      }
      if (!token) return;
      if (!existing[pool]) existing[pool] = [];
      const existingSet = new Set(existing[pool].map(t => typeof t === 'string' ? t : t.token));
      if (!existingSet.has(token)) {
        existing[pool].push({ token: token });
        added++;
      }
    });

    const saveRes = await fetch('/v1/admin/tokens', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...buildAuthHeaders(apiKey) },
      body: JSON.stringify(existing)
    });

    if (saveRes.ok) {
      closeImportModal();
      await loadAccountData();
      showToast(`成功导入 ${added} 个 Token`, 'success');
    } else {
      showToast('导入失败', 'error');
    }
  } catch (e) {
    console.error(e);
    showToast('请求失败', 'error');
  }
}

// ========== 导出功能 ==========

function exportTokens() {
  if (!allTokens.length) { showToast('没有账号可导出', 'info'); return; }
  let csv = 'token,pool,status,alive,nsfw,quota\n';
  allTokens.forEach(t => {
    const nsfw = t.tags && t.tags.includes('nsfw') ? 'yes' : 'no';
    const alive = t.alive === true ? 'yes' : (t.alive === false ? 'no' : 'unknown');
    csv += `${t.token},${t.pool},${t.status},${alive},${nsfw},${t.quota}\n`;
  });
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `grok2api_tokens_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  showToast(`已导出 ${allTokens.length} 个账号`, 'success');
}

// ========== 确认对话框 ==========

function showConfirm(title, message, onConfirm) {
  byId('confirm-title').textContent = title;
  byId('confirm-message').textContent = message;
  const modal = byId('confirm-overlay');
  modal.classList.remove('hidden');
  requestAnimationFrame(() => modal.classList.add('is-open'));
  pendingConfirmFn = onConfirm;
}
function closeConfirm() {
  const modal = byId('confirm-overlay');
  modal.classList.remove('is-open');
  setTimeout(() => modal.classList.add('hidden'), 200);
  pendingConfirmFn = null;
}
function confirmAction() {
  closeConfirm();
  if (pendingConfirmFn) pendingConfirmFn();
}

window.onload = init;

let apiKey = '';
let allTokens = [];
let currentFilter = 'all';
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
    const res = await fetch('/v1/admin/tokens', {
      headers: buildAuthHeaders(apiKey)
    });
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
          last_alive_check_at: t.last_alive_check_at,
          fail_count: t.fail_count || 0,
        });
      });
    });

    updateStats();
    renderTable();
  } catch (e) {
    console.error(e);
    showToast('加载数据失败', 'error');
  }
}

function updateStats() {
  const total = allTokens.length;
  const alive = allTokens.filter(t => t.alive === true).length;
  const dead = allTokens.filter(t => t.alive === false || t.status === 'expired').length;
  const active = allTokens.filter(t => t.status === 'active').length;
  const cooling = allTokens.filter(t => t.status === 'cooling').length;
  const disabled = allTokens.filter(t => t.status === 'disabled' || t.status === 'expired').length;

  byId('stat-total').textContent = total;
  byId('stat-alive').textContent = alive;
  byId('stat-expired').textContent = dead;
  byId('stat-active').textContent = active;
  byId('stat-cooling').textContent = cooling;
  byId('stat-disabled').textContent = disabled;

  // Tab counts
  byId('tab-all').textContent = total;
  byId('tab-active').textContent = active;
  byId('tab-cooling').textContent = cooling;
  byId('tab-expired').textContent = disabled;
  byId('tab-alive').textContent = alive;
  byId('tab-dead').textContent = dead;
}

function filterByStatus(status) {
  currentFilter = status;
  document.querySelectorAll('#status-tabs .tab-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === status);
  });
  renderTable();
}

function getFilteredTokens() {
  if (currentFilter === 'all') return allTokens;
  if (currentFilter === 'active') return allTokens.filter(t => t.status === 'active');
  if (currentFilter === 'cooling') return allTokens.filter(t => t.status === 'cooling');
  if (currentFilter === 'expired') return allTokens.filter(t => t.status === 'disabled' || t.status === 'expired');
  if (currentFilter === 'alive') return allTokens.filter(t => t.alive === true);
  if (currentFilter === 'dead') return allTokens.filter(t => t.alive === false || t.status === 'expired');
  return allTokens;
}

function renderTable() {
  const tbody = byId('account-table-body');
  const empty = byId('empty-state');
  const filtered = getFilteredTokens();

  if (filtered.length === 0) {
    tbody.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');

  const fragment = document.createDocumentFragment();
  filtered.forEach((item, idx) => {
    const tr = document.createElement('tr');

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

    // 状态
    const tdStatus = document.createElement('td');
    tdStatus.className = 'text-center';
    let statusClass = 'badge-gray';
    if (item.status === 'active') statusClass = 'badge-green';
    else if (item.status === 'cooling') statusClass = 'badge-orange';
    else if (item.status === 'expired') statusClass = 'badge-red';
    tdStatus.innerHTML = `<span class="badge ${statusClass}">${item.status}</span>`;

    // 可用
    const tdAlive = document.createElement('td');
    tdAlive.className = 'text-center text-sm';
    if (item.alive === true) {
      tdAlive.innerHTML = '<span class="text-green-600 font-bold" title="可用">&#10003;</span>';
    } else if (item.alive === false) {
      tdAlive.innerHTML = '<span class="text-red-600 font-bold" title="不可用">&#10007;</span>';
    } else {
      tdAlive.innerHTML = '<span class="text-gray-400" title="未检测">-</span>';
    }

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
    tdActions.innerHTML = `
      <div class="flex items-center justify-center gap-2">
        <button onclick="checkSingleAlive('${item.token}')" class="p-1 text-gray-400 hover:text-green-600 rounded" title="检测可用性">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>
        </button>
        <button onclick="deleteSingle('${item.token}', '${item.pool}')" class="p-1 text-gray-400 hover:text-red-600 rounded" title="删除">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
        </button>
      </div>
    `;

    tr.appendChild(tdToken);
    tr.appendChild(tdPool);
    tr.appendChild(tdStatus);
    tr.appendChild(tdAlive);
    tr.appendChild(tdQuota);
    tr.appendChild(tdLastCheck);
    tr.appendChild(tdActions);
    fragment.appendChild(tr);
  });

  tbody.replaceChildren(fragment);
}

async function checkAllAlive() {
  const btn = byId('btn-check-all');
  const progress = byId('check-progress');
  if (!allTokens.length) {
    showToast('没有账号需要检测', 'info');
    return;
  }

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

function cleanExpired() {
  const expired = allTokens.filter(t => t.alive === false || t.status === 'expired');
  if (expired.length === 0) {
    showToast('没有失效账号需要清理', 'info');
    return;
  }
  showConfirm(
    '清理失效账号',
    `确认删除 ${expired.length} 个失效账号？此操作不可撤销。`,
    async () => {
      try {
        // 构建删除后的 token 数据（排除失效的）
        const res = await fetch('/v1/admin/tokens', {
          headers: buildAuthHeaders(apiKey)
        });
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
        } else {
          showToast('清理失败', 'error');
        }
      } catch (e) {
        console.error(e);
        showToast('请求失败', 'error');
      }
    }
  );
}

async function deleteSingle(token, pool) {
  showConfirm('删除账号', `确认删除此 Token？`, async () => {
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

      if (saveRes.ok) {
        await loadAccountData();
        showToast('已删除', 'success');
      } else {
        showToast('删除失败', 'error');
      }
    } catch (e) {
      console.error(e);
      showToast('请求失败', 'error');
    }
  });
}

// 确认对话框
function showConfirm(title, message, onConfirm) {
  byId('confirm-title').textContent = title;
  byId('confirm-message').textContent = message;
  byId('confirm-overlay').classList.remove('hidden');
  pendingConfirmFn = onConfirm;
}
function closeConfirm() {
  byId('confirm-overlay').classList.add('hidden');
  pendingConfirmFn = null;
}
function confirmAction() {
  closeConfirm();
  if (pendingConfirmFn) pendingConfirmFn();
}

window.onload = init;

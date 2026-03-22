let apiKey = "";
let allTokens = [];
let pendingConfirmFn = null;
let importController = null;
let editingToken = null;
const REAL_QUOTA_DISPLAY_MODE_KEY = "accountRealQuotaDisplayMode";
const REAL_QUOTA_DISPLAY_MODES = {
  MODEL: "model",
  SHORTCUT: "shortcut",
};
let realQuotaDisplayMode = REAL_QUOTA_DISPLAY_MODES.MODEL;

const byId = (id) => document.getElementById(id);
const CHECK_ALL_BUTTON_HTML = `
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
    <polyline points="22 4 12 14.01 9 11.01"></polyline>
  </svg>
  全部检测
`;

const REFRESH_REAL_QUOTA_BUTTON_HTML = `
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M21 12a9 9 0 1 1-3.2-6.91"></path>
    <polyline points="21 3 21 9 15 9"></polyline>
  </svg>
  真实额度`;

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function formatTime(timestamp) {
  if (!timestamp) return "-";
  const date = new Date(timestamp);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

async function readJsonResponse(response) {
  const text = await response.text();
  if (!text) return null;

  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function hasNsfwTag(item) {
  return Array.isArray(item.tags) && item.tags.includes("nsfw");
}

function shortenToken(token) {
  if (token.length <= 24) return token;
  return `${token.slice(0, 8)}...${token.slice(-16)}`;
}

function createIconButton({ title, className, svg, onClick }) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.title = title;
  button.innerHTML = svg;
  button.addEventListener("click", onClick);
  return button;
}

function getRealQuotaHelper() {
  return window.AccountRealQuota &&
    typeof window.AccountRealQuota.getRealQuotaState === "function"
    ? window.AccountRealQuota
    : null;
}

function normalizeRealQuotaDisplayMode(value) {
  const helper = getRealQuotaHelper();
  if (helper && typeof helper.normalizeDisplayMode === "function") {
    return helper.normalizeDisplayMode(value);
  }
  return value === REAL_QUOTA_DISPLAY_MODES.SHORTCUT
    ? REAL_QUOTA_DISPLAY_MODES.SHORTCUT
    : REAL_QUOTA_DISPLAY_MODES.MODEL;
}

function loadRealQuotaDisplayMode() {
  let stored = "";
  try {
    stored = localStorage.getItem(REAL_QUOTA_DISPLAY_MODE_KEY) || "";
  } catch {
    stored = "";
  }

  const helper = getRealQuotaHelper();
  const defaultMode = helper && helper.DEFAULT_DISPLAY_MODE
    ? helper.DEFAULT_DISPLAY_MODE
    : REAL_QUOTA_DISPLAY_MODES.MODEL;
  return normalizeRealQuotaDisplayMode(stored || defaultMode);
}

function saveRealQuotaDisplayMode(value) {
  try {
    localStorage.setItem(REAL_QUOTA_DISPLAY_MODE_KEY, value);
  } catch {
    // Ignore storage errors in private mode or locked-down browsers.
  }
}

function syncRealQuotaDisplayModeToggle() {
  document.querySelectorAll("[data-real-quota-display-mode]").forEach((button) => {
    const isActive = button.dataset.realQuotaDisplayMode === realQuotaDisplayMode;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-pressed", String(isActive));
  });
}

function setRealQuotaDisplayMode(value) {
  const nextMode = normalizeRealQuotaDisplayMode(value);
  const changed = nextMode !== realQuotaDisplayMode;
  realQuotaDisplayMode = nextMode;
  saveRealQuotaDisplayMode(nextMode);
  syncRealQuotaDisplayModeToggle();

  if (changed) {
    renderTable();
  }
}

function initRealQuotaDisplayModeToggle() {
  realQuotaDisplayMode = loadRealQuotaDisplayMode();
  document.querySelectorAll("[data-real-quota-display-mode]").forEach((button) => {
    if (button.dataset.bound === "1") return;
    button.dataset.bound = "1";
    button.addEventListener("click", () => {
      setRealQuotaDisplayMode(button.dataset.realQuotaDisplayMode || "");
    });
  });
  syncRealQuotaDisplayModeToggle();
}

function ensureImportController() {
  if (importController || !window.AccountImport) return importController;

  importController = window.AccountImport.createController({
    byId,
    getApiKey: () => apiKey,
    getAuthHeaders: () => buildAuthHeaders(apiKey),
    onReload: loadAccountData,
    showToast,
  });

  return importController;
}

async function fetchTokenState() {
  const response = await fetch("/v1/admin/tokens", {
    headers: buildAuthHeaders(apiKey),
  });
  const data = await readJsonResponse(response);

  if (response.status === 401) {
    logout();
    throw new Error("未授权");
  }

  if (!response.ok) {
    throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
  }

  return data || {};
}

async function saveTokenState(tokensByPool) {
  const response = await fetch("/v1/admin/tokens", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...buildAuthHeaders(apiKey),
    },
    body: JSON.stringify(tokensByPool),
  });
  const data = await readJsonResponse(response);

  if (!response.ok) {
    throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
  }

  return data || {};
}

function updateSelectAllState() {
  const checkbox = byId("select-all");
  if (!checkbox) return;

  const selectedCount = allTokens.filter((item) => item._selected).length;
  checkbox.checked = allTokens.length > 0 && selectedCount === allTokens.length;
  checkbox.indeterminate = selectedCount > 0 && selectedCount < allTokens.length;
}

function setEmptyState(isEmpty) {
  const empty = byId("empty-state");
  if (empty) empty.classList.toggle("hidden", !isEmpty);

  if (!isEmpty) return;

  const checkbox = byId("select-all");
  if (checkbox) {
    checkbox.checked = false;
    checkbox.indeterminate = false;
  }
}

function getAliveDisplay(item) {
  if (item.status === "expired") {
    return '<span class="text-red-600 font-bold" title="失效">&#10007;</span>';
  }
  if (item.status === "cooling") {
    return '<span class="text-orange-500 font-bold" title="限流">&#9724;</span>';
  }
  if (item.status === "disabled") {
    return '<span class="text-gray-400" title="已禁用">&#9724;</span>';
  }
  if (item.alive === true) {
    return '<span class="text-green-600 font-bold" title="可用">&#10003;</span>';
  }
  if (item.alive === false) {
    return '<span class="text-red-600 font-bold" title="不可用">&#10007;</span>';
  }
  return '<span class="text-gray-400" title="未检测">-</span>';
}

function getRealQuotaCardIconSvg(symbol) {
  if (symbol === "image") {
    return `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="3.5" y="4.5" width="17" height="15" rx="3"></rect>
        <circle cx="9" cy="10" r="1.5"></circle>
        <path d="M6.5 17l4.2-4.2a1 1 0 0 1 1.4 0L14 14.7"></path>
        <path d="M12.5 13.5l1.8-1.8a1 1 0 0 1 1.4 0l2.8 2.8"></path>
      </svg>
    `;
  }

  if (symbol === "video") {
    return `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <rect x="3.5" y="5.5" width="12" height="13" rx="3"></rect>
        <path d="M15.5 10l4-2.5a.7.7 0 0 1 1.1.6v8a.7.7 0 0 1-1.1.6l-4-2.5"></path>
      </svg>
    `;
  }

  return "";
}

function buildFallbackRealQuotaState() {
  return {
    displayMode: REAL_QUOTA_DISPLAY_MODES.MODEL,
    label: "未查询",
    badgeClass: "badge-gray",
    rows: [
      [
        {
          markerText: "3",
          tooltipLabel: "3额度",
          tone: "text-3",
          value: "-",
          detail: "",
          status: "empty",
          symbol: "",
        },
        {
          markerText: "4",
          tooltipLabel: "4额度",
          tone: "text-4",
          value: "-",
          detail: "",
          status: "empty",
          symbol: "",
        },
      ],
      [
        {
          tooltipLabel: "图片额度",
          tone: "image",
          value: "-",
          detail: "",
          status: "empty",
          symbol: "image",
        },
        {
          tooltipLabel: "视频额度",
          tone: "video",
          value: "-",
          detail: "",
          status: "empty",
          symbol: "video",
        },
      ],
    ],
    note: "点击刷新真实额度",
    meta: "",
    error: "",
    title: "",
  };
}

function createRealQuotaItem(card) {
  const itemNode = document.createElement("div");
  itemNode.className = `real-quota-item real-quota-item--${card.tone || "neutral"}`;

  const titleParts = [
    `${card.tooltipLabel || card.markerText || ""}: ${card.value}${card.detail ? ` ${card.detail}` : ""}`,
  ];
  if (card.sourceModelName && card.sourceModelName !== card.modelName) {
    titleParts.push(`来源 ${card.sourceModelName}`);
  }
  if (card.rawError) {
    titleParts.push(`错误 ${card.rawError}`);
  }
  itemNode.title = titleParts.join(" | ");

  if (card.symbol) {
    const symbol = document.createElement("span");
    symbol.className = "real-quota-symbol";
    symbol.innerHTML = getRealQuotaCardIconSvg(card.symbol);
    symbol.setAttribute("aria-hidden", "true");
    itemNode.appendChild(symbol);
  } else {
    const marker = document.createElement("span");
    marker.className = "real-quota-marker";
    marker.textContent = card.markerText || "-";
    itemNode.appendChild(marker);
  }

  const pill = document.createElement("span");
  pill.className = `real-quota-pill real-quota-pill--${card.tone || "neutral"} real-quota-pill--state-${card.status || "empty"}`;
  pill.textContent = card.value || "-";
  itemNode.appendChild(pill);

  if (card.detail) {
    const detail = document.createElement("span");
    detail.className = "real-quota-detail";
    detail.textContent = card.detail;
    itemNode.appendChild(detail);
  }

  return itemNode;
}

function createRealQuotaCell(item) {
  const cell = document.createElement("td");
  cell.className = "text-left real-quota-cell";

  const helper = getRealQuotaHelper();
  const state = helper
    ? helper.getRealQuotaState(item)
    : buildFallbackRealQuotaState();

  const wrapper = document.createElement("div");
  wrapper.className = "real-quota-wrap";
  if (state.title) {
    wrapper.title = state.title;
  }

  const main = document.createElement("div");
  main.className = "real-quota-main";

  const badge = document.createElement("span");
  badge.className = `badge ${state.badgeClass || "badge-gray"}`;
  badge.textContent = state.label || "未查询";
  main.appendChild(badge);

  if (state.meta) {
    const meta = document.createElement("span");
    meta.className = "real-quota-meta";
    meta.textContent = state.meta;
    main.appendChild(meta);
  }

  wrapper.appendChild(main);

  const lines = document.createElement("div");
  lines.className = "real-quota-lines";

  (Array.isArray(state.rows) ? state.rows : []).forEach((rowCards) => {
    const line = document.createElement("div");
    line.className = "real-quota-line";
    (Array.isArray(rowCards) ? rowCards : []).forEach((card, index) => {
      if (index > 0) {
        const separator = document.createElement("span");
        separator.className = "real-quota-item-sep";
        separator.textContent = "|";
        line.appendChild(separator);
      }
      line.appendChild(createRealQuotaItem(card || {}));
    });
    lines.appendChild(line);
  });

  wrapper.appendChild(lines);

  if (state.note) {
    const note = document.createElement("div");
    note.className = "real-quota-note";
    note.textContent = state.note;
    wrapper.appendChild(note);
  }

  if (state.error) {
    const error = document.createElement("div");
    error.className = "real-quota-error";
    error.textContent = state.error;
    wrapper.appendChild(error);
  }

  cell.appendChild(wrapper);
  return cell;
}

function renderTable() {
  const tbody = byId("account-table-body");
  if (!tbody) return;

  if (allTokens.length === 0) {
    tbody.replaceChildren();
    setEmptyState(true);
    return;
  }

  setEmptyState(false);
  const fragment = document.createDocumentFragment();

  allTokens.forEach((item, index) => {
    const row = document.createElement("tr");
    row.classList.toggle("row-selected", Boolean(item._selected));

    const checkCell = document.createElement("td");
    checkCell.className = "text-center";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "checkbox";
    checkbox.checked = Boolean(item._selected);
    checkbox.addEventListener("change", () => toggleSelect(index));
    checkCell.appendChild(checkbox);

    const tokenCell = document.createElement("td");
    tokenCell.className = "text-left account-token-cell";
    tokenCell.innerHTML = `
      <span class="font-mono text-xs text-gray-500" title="${escapeHtml(item.token)}">
        ${escapeHtml(shortenToken(item.token))}
      </span>
    `;

    const emailCell = document.createElement("td");
    emailCell.className = "text-left text-sm account-email-cell";
    emailCell.textContent = item.email || "-";
    emailCell.title = item.email || "";

    const poolCell = document.createElement("td");
    poolCell.className = "text-center";
    poolCell.innerHTML = `<span class="badge badge-gray">${escapeHtml(item.pool)}</span>`;

    const aliveCell = document.createElement("td");
    aliveCell.className = "text-center text-sm";
    aliveCell.innerHTML = getAliveDisplay(item);

    const nsfwCell = document.createElement("td");
    nsfwCell.className = "text-center text-sm";
    nsfwCell.innerHTML = hasNsfwTag(item)
      ? '<span class="text-purple-600 font-bold" title="NSFW 已开启">&#10003;</span>'
      : '<span class="text-gray-400" title="NSFW 未开启">&#10007;</span>';

    const quotaCell = document.createElement("td");
    quotaCell.className = "text-center font-mono text-xs";
    quotaCell.textContent = String(item.quota || 0);

    const realQuotaCell = createRealQuotaCell(item);

    const lastCheckCell = document.createElement("td");
    lastCheckCell.className = "text-center text-xs text-gray-500 account-last-check-cell";
    lastCheckCell.textContent = formatTime(item.last_alive_check_at);

    const actionCell = document.createElement("td");
    actionCell.className = "text-center";
    const actionGroup = document.createElement("div");
    actionGroup.className = "account-actions flex items-center justify-center gap-1";

    actionGroup.appendChild(
      createIconButton({
        title: "编辑",
        className: "p-1 text-gray-400 hover:text-blue-600 rounded",
        svg: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"></path><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"></path></svg>',
        onClick: () => openEditModal(index),
      })
    );

    actionGroup.appendChild(
      createIconButton({
        title: "检测",
        className: "p-1 text-gray-400 hover:text-green-600 rounded",
        svg: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>',
        onClick: () => checkSingleAlive(item.token),
      })
    );

    actionGroup.appendChild(
      createIconButton({
        title: "刷新真实额度",
        className: "p-1 text-gray-400 hover:text-purple-600 rounded",
        svg: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3.2-6.91"></path><polyline points="21 3 21 9 15 9"></polyline></svg>',
        onClick: () =>
          refreshRealQuota([item.token], {
            progressMessage: "正在刷新当前账号的真实额度...",
            successMessage: "真实额度已刷新",
          }),
      })
    );

    actionGroup.appendChild(
      createIconButton({
        title: hasNsfwTag(item) ? "关闭 NSFW" : "开启 NSFW",
        className: `p-1 rounded ${
          hasNsfwTag(item)
            ? "text-purple-500 hover:text-gray-400"
            : "text-gray-400 hover:text-purple-500"
        }`,
        svg: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>',
        onClick: () => toggleSingleNSFW(index),
      })
    );

    actionGroup.appendChild(
      createIconButton({
        title: item.status === "disabled" ? "启用" : "禁用",
        className: `p-1 rounded text-gray-400 ${
          item.status === "disabled" ? "hover:text-green-600" : "hover:text-orange-600"
        }`,
        svg:
          item.status === "disabled"
            ? '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>'
            : '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>',
        onClick: () => toggleSingleStatus(index),
      })
    );

    actionGroup.appendChild(
      createIconButton({
        title: "删除",
        className: "p-1 text-gray-400 hover:text-red-600 rounded",
        svg: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>',
        onClick: () => deleteSingle(item.token),
      })
    );

    actionCell.appendChild(actionGroup);

    row.appendChild(checkCell);
    row.appendChild(tokenCell);
    row.appendChild(emailCell);
    row.appendChild(poolCell);
    row.appendChild(aliveCell);
    row.appendChild(nsfwCell);
    row.appendChild(quotaCell);
    row.appendChild(realQuotaCell);
    row.appendChild(lastCheckCell);
    row.appendChild(actionCell);
    fragment.appendChild(row);
  });

  tbody.replaceChildren(fragment);
  updateSelectAllState();
}

async function init() {
  apiKey = await ensureAdminKey();
  if (apiKey === null) return;

  realQuotaDisplayMode = REAL_QUOTA_DISPLAY_MODES.MODEL;
  ensureImportController();
  await loadAccountData();
}

async function loadAccountData() {
  try {
    const data = await fetchTokenState();
    const tokensByPool = data.tokens || {};

    allTokens = [];
    Object.entries(tokensByPool).forEach(([pool, list]) => {
      if (!Array.isArray(list)) return;

      list.forEach((item) => {
        const tokenInfo = typeof item === "string" ? { token: item } : item || {};
        allTokens.push({
          token: tokenInfo.token || "",
          email: tokenInfo.email || "",
          pool,
          status: tokenInfo.status || "active",
          alive: tokenInfo.alive ?? null,
          quota: tokenInfo.quota || 0,
          real_tier: tokenInfo.real_tier || "",
          real_tier_name: tokenInfo.real_tier_name || "",
          real_quota:
            tokenInfo.real_quota && typeof tokenInfo.real_quota === "object"
              ? tokenInfo.real_quota
              : null,
          last_real_quota_check_at: tokenInfo.last_real_quota_check_at,
          last_real_quota_error: tokenInfo.last_real_quota_error || "",
          tags: Array.isArray(tokenInfo.tags) ? tokenInfo.tags : [],
          last_alive_check_at: tokenInfo.last_alive_check_at,
          _selected: false,
        });
      });
    });

    renderTable();
  } catch (error) {
    if (error.message === "未授权") return;
    console.error(error);
    showToast(`加载数据失败：${error.message}`, "error");
  }
}

function toggleSelect(index) {
  if (!allTokens[index]) return;
  allTokens[index]._selected = !allTokens[index]._selected;
  renderTable();
}

function toggleSelectAll() {
  const checkbox = byId("select-all");
  const checked = Boolean(checkbox && checkbox.checked);
  allTokens.forEach((item) => {
    item._selected = checked;
  });
  renderTable();
}

function getSelected() {
  return allTokens.filter((item) => item._selected);
}

function getRealQuotaTargets() {
  const selected = getSelected();
  if (selected.length > 0) {
    return selected.map((item) => item.token);
  }
  return allTokens.map((item) => item.token);
}

async function updateTokenStatus(tokens, newStatus) {
  try {
    const data = await fetchTokenState();
    const tokensByPool = data.tokens || {};
    const targetTokens = new Set(tokens.map((item) => item.token));

    Object.entries(tokensByPool).forEach(([pool, list]) => {
      if (!Array.isArray(list)) return;

      tokensByPool[pool] = list.map((entry) => {
        const tokenInfo = typeof entry === "string" ? { token: entry } : { ...entry };
        if (targetTokens.has(tokenInfo.token)) tokenInfo.status = newStatus;
        return tokenInfo;
      });
    });

    await saveTokenState(tokensByPool);
    return true;
  } catch (error) {
    console.error(error);
    showToast(`操作失败：${error.message}`, "error");
    return false;
  }
}

async function batchEnable() {
  const selected = getSelected();
  if (selected.length === 0) {
    showToast("请先选择账号", "info");
    return;
  }

  const ok = await updateTokenStatus(selected, "active");
  if (!ok) return;

  await loadAccountData();
  showToast(`已启用 ${selected.length} 个账号`, "success");
}

async function batchDisable() {
  const selected = getSelected();
  if (selected.length === 0) {
    showToast("请先选择账号", "info");
    return;
  }

  const ok = await updateTokenStatus(selected, "disabled");
  if (!ok) return;

  await loadAccountData();
  showToast(`已禁用 ${selected.length} 个账号`, "success");
}

async function toggleSingleStatus(index) {
  const item = allTokens[index];
  if (!item) return;

  const nextStatus = item.status === "disabled" ? "active" : "disabled";
  const ok = await updateTokenStatus([item], nextStatus);
  if (!ok) return;

  await loadAccountData();
  showToast(nextStatus === "active" ? "账号已启用" : "账号已禁用", "success");
}

async function requestNsfwEnable(tokens) {
  const response = await fetch("/v1/admin/tokens/nsfw/enable", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...buildAuthHeaders(apiKey),
    },
    body: JSON.stringify({ tokens }),
  });
  const data = await readJsonResponse(response);

  if (!response.ok || !data || data.status !== "success") {
    throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
  }

  return data;
}

async function removeNsfwTag(token) {
  const data = await fetchTokenState();
  const tokensByPool = data.tokens || {};

  Object.entries(tokensByPool).forEach(([pool, list]) => {
    if (!Array.isArray(list)) return;

    tokensByPool[pool] = list.map((entry) => {
      const tokenInfo = typeof entry === "string" ? { token: entry } : { ...entry };
      if (tokenInfo.token === token) {
        tokenInfo.tags = (tokenInfo.tags || []).filter((tag) => tag !== "nsfw");
      }
      return tokenInfo;
    });
  });

  await saveTokenState(tokensByPool);
}

async function toggleSingleNSFW(index) {
  const item = allTokens[index];
  if (!item) return;

  try {
    if (hasNsfwTag(item)) {
      await removeNsfwTag(item.token);
      await loadAccountData();
      showToast("NSFW 已关闭", "success");
      return;
    }

    await requestNsfwEnable([item.token]);
    await loadAccountData();
    showToast("NSFW 已开启", "success");
  } catch (error) {
    console.error(error);
    showToast(`NSFW 操作失败：${error.message}`, "error");
  }
}

async function batchEnableNSFW() {
  const selected = getSelected();
  if (selected.length === 0) {
    showToast("请先选择账号", "info");
    return;
  }

  const targets = selected.filter((item) => !hasNsfwTag(item)).map((item) => item.token);
  if (targets.length === 0) {
    showToast("选中的账号都已开启 NSFW", "info");
    return;
  }

  try {
    await requestNsfwEnable(targets);
    await loadAccountData();
    showToast(`已为 ${targets.length} 个账号开启 NSFW`, "success");
  } catch (error) {
    console.error(error);
    showToast(`开启 NSFW 失败：${error.message}`, "error");
  }
}

async function requestRealQuotaRefresh(tokens) {
  const response = await fetch("/v1/admin/tokens/real-quota/refresh", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...buildAuthHeaders(apiKey),
    },
    body: JSON.stringify(Array.isArray(tokens) && tokens.length ? { tokens } : {}),
  });
  const data = await readJsonResponse(response);

  if (!response.ok || !data || data.status !== "success") {
    throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
  }

  return data;
}

async function refreshRealQuota(tokens, options = {}) {
  const button = options.button || null;
  const progress = byId("check-progress");
  const buttonResetHtml = options.buttonResetHtml || REFRESH_REAL_QUOTA_BUTTON_HTML;
  const progressMessage = options.progressMessage || "正在刷新真实额度...";
  const successMessage = options.successMessage || "";

  if (button) {
    button.disabled = true;
    button.textContent = "刷新中...";
  }
  if (progress) {
    progress.textContent = progressMessage;
    progress.classList.remove("hidden");
  }

  try {
    const data = await requestRealQuotaRefresh(tokens);
    const summary = data.summary || {};
    await loadAccountData();

    const okCount = Number(summary.ok || 0);
    const failCount = Number(summary.fail || 0);
    showToast(
      successMessage || `真实额度刷新完成：成功 ${okCount}，失败 ${failCount}`,
      failCount > 0 ? "warning" : "success"
    );
    return data;
  } catch (error) {
    console.error(error);
    showToast(`真实额度刷新失败：${error.message}`, "error");
    throw error;
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = buttonResetHtml;
    }
    if (progress) {
      progress.textContent = "";
      progress.classList.add("hidden");
    }
  }
}

async function refreshSelectedOrAllRealQuota() {
  const targets = getRealQuotaTargets();
  if (targets.length === 0) {
    showToast("没有账号可以刷新真实额度", "info");
    return;
  }

  const selectedCount = getSelected().length;
  const button = byId("btn-refresh-real-quota");
  await refreshRealQuota(targets, {
    button,
    progressMessage:
      selectedCount > 0
        ? `正在刷新 ${selectedCount} 个选中账号的真实额度...`
        : `正在刷新全部 ${targets.length} 个账号的真实额度...`,
  });
}

async function checkAllAlive() {
  if (allTokens.length === 0) {
    showToast("没有账号需要检测", "info");
    return;
  }

  const button = byId("btn-check-all");
  const progress = byId("check-progress");

  if (button) {
    button.disabled = true;
    button.textContent = "检测中...";
  }
  if (progress) {
    progress.textContent = "正在检测所有账号...";
    progress.classList.remove("hidden");
  }

  try {
    const response = await fetch("/v1/admin/tokens/alive", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...buildAuthHeaders(apiKey),
      },
      body: JSON.stringify({
        tokens: allTokens.map((item) => item.token),
      }),
    });
    const data = await readJsonResponse(response);

    if (!response.ok || !data || data.status !== "success") {
      throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
    }

    const results = data.results || {};
    let okCount = 0;
    let failCount = 0;
    Object.values(results).forEach((value) => {
      if (value === true) {
        okCount += 1;
      } else {
        failCount += 1;
      }
    });

    await loadAccountData();
    showToast(`检测完成：${okCount} 可用，${failCount} 不可用`, "success");
  } catch (error) {
    console.error(error);
    showToast(`检测失败：${error.message}`, "error");
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = CHECK_ALL_BUTTON_HTML;
    }
    if (progress) {
      progress.textContent = "";
      progress.classList.add("hidden");
    }
  }
}

async function checkSingleAlive(token) {
  try {
    const response = await fetch("/v1/admin/tokens/alive", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...buildAuthHeaders(apiKey),
      },
      body: JSON.stringify({ token }),
    });
    const data = await readJsonResponse(response);

    if (!response.ok || !data || data.status !== "success") {
      throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
    }

    const alive = data.results && data.results[token];
    await loadAccountData();

    if (alive === true) {
      showToast("Token 可用", "success");
    } else if (alive === false) {
      showToast("Token 不可用", "error");
    } else {
      showToast("检测结果未知", "warning");
    }
  } catch (error) {
    console.error(error);
    showToast(`检测失败：${error.message}`, "error");
  }
}

function showConfirm(title, message, onConfirm) {
  const modal = byId("confirm-overlay");
  const titleNode = byId("confirm-title");
  const messageNode = byId("confirm-message");
  if (!modal || !titleNode || !messageNode) return;

  pendingConfirmFn = onConfirm;
  titleNode.textContent = title;
  messageNode.textContent = message;

  modal.classList.remove("hidden");
  requestAnimationFrame(() => {
    modal.classList.add("is-open");
  });
}

function closeConfirm() {
  const modal = byId("confirm-overlay");
  if (modal) {
    modal.classList.remove("is-open");
    setTimeout(() => {
      modal.classList.add("hidden");
    }, 200);
  }

  pendingConfirmFn = null;
}

function confirmAction() {
  const confirmFn = pendingConfirmFn;
  closeConfirm();
  if (typeof confirmFn === "function") confirmFn();
}

function cleanExpired() {
  const expired = allTokens.filter((item) => item.alive === false || item.status === "expired");
  if (expired.length === 0) {
    showToast("没有失效账号需要清理", "info");
    return;
  }

  showConfirm("清理失效账号", `确认删除 ${expired.length} 个失效账号？`, async () => {
    try {
      const data = await fetchTokenState();
      const tokensByPool = data.tokens || {};
      const expiredTokens = new Set(expired.map((item) => item.token));

      Object.entries(tokensByPool).forEach(([pool, list]) => {
        if (!Array.isArray(list)) return;
        tokensByPool[pool] = list.filter((entry) => {
          const tokenInfo = typeof entry === "string" ? { token: entry } : entry || {};
          return !expiredTokens.has(tokenInfo.token);
        });
      });

      await saveTokenState(tokensByPool);
      await loadAccountData();
      showToast(`已清理 ${expired.length} 个失效账号`, "success");
    } catch (error) {
      console.error(error);
      showToast(`清理失败：${error.message}`, "error");
    }
  });
}

function deleteSingle(token) {
  showConfirm("删除账号", "确认删除此 Token？", async () => {
    try {
      const data = await fetchTokenState();
      const tokensByPool = data.tokens || {};

      Object.entries(tokensByPool).forEach(([pool, list]) => {
        if (!Array.isArray(list)) return;
        tokensByPool[pool] = list.filter((entry) => {
          const tokenInfo = typeof entry === "string" ? { token: entry } : entry || {};
          return tokenInfo.token !== token;
        });
      });

      await saveTokenState(tokensByPool);
      await loadAccountData();
      showToast("账号已删除", "success");
    } catch (error) {
      console.error(error);
      showToast(`删除失败：${error.message}`, "error");
    }
  });
}

function openEditModal(index) {
  const item = allTokens[index];
  const modal = byId("edit-modal");
  if (!item || !modal) return;

  editingToken = item.token;
  const tokenNode = byId("edit-token-display");
  const emailInput = byId("edit-email");
  const poolInput = byId("edit-pool");

  if (tokenNode) tokenNode.textContent = item.token;
  if (emailInput) emailInput.value = item.email || "";
  if (poolInput) poolInput.value = item.pool || "ssoBasic";

  modal.classList.remove("hidden");
  requestAnimationFrame(() => {
    modal.classList.add("is-open");
  });
}

function closeEditModal() {
  const modal = byId("edit-modal");
  editingToken = null;
  if (!modal) return;

  modal.classList.remove("is-open");
  setTimeout(() => {
    modal.classList.add("hidden");
  }, 200);
}

async function saveEditToken() {
  if (!editingToken) return;

  const emailInput = byId("edit-email");
  const poolInput = byId("edit-pool");
  const nextEmail = (emailInput && emailInput.value.trim()) || "";
  const nextPool = (poolInput && poolInput.value) || "ssoBasic";

  try {
    const data = await fetchTokenState();
    const tokensByPool = data.tokens || {};
    let targetItem = null;
    let previousPool = null;

    Object.entries(tokensByPool).forEach(([pool, list]) => {
      if (!Array.isArray(list)) return;

      tokensByPool[pool] = list.filter((entry) => {
        const tokenInfo = typeof entry === "string" ? { token: entry } : { ...entry };
        if (tokenInfo.token !== editingToken) return true;
        targetItem = tokenInfo;
        previousPool = pool;
        return false;
      });
    });

    if (!targetItem) {
      throw new Error("未找到要编辑的账号");
    }

    targetItem.email = nextEmail;
    if (!tokensByPool[nextPool]) tokensByPool[nextPool] = [];

    const duplicateIndex = tokensByPool[nextPool].findIndex((entry) => {
      const tokenInfo = typeof entry === "string" ? { token: entry } : entry || {};
      return tokenInfo.token === editingToken;
    });

    if (duplicateIndex >= 0) {
      const existingEntry = tokensByPool[nextPool][duplicateIndex];
      const normalizedExisting =
        typeof existingEntry === "string" ? { token: existingEntry } : existingEntry || {};
      tokensByPool[nextPool][duplicateIndex] = {
        ...normalizedExisting,
        ...targetItem,
      };
    } else {
      tokensByPool[nextPool].push(targetItem);
    }

    await saveTokenState(tokensByPool);
    closeEditModal();
    await loadAccountData();
    showToast(
      previousPool === nextPool ? "账号信息已更新" : "账号已更新并移动到新的 Pool",
      "success"
    );
  } catch (error) {
    console.error(error);
    showToast(`保存失败：${error.message}`, "error");
  }
}

function downloadFile(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function exportTokens() {
  if (allTokens.length === 0) {
    showToast("没有账号可导出", "info");
    return;
  }

  const lines = ["token,pool,email,status,alive,nsfw,quota"];
  allTokens.forEach((item) => {
    lines.push(
      [
        item.token,
        item.pool,
        item.email || "",
        item.status,
        item.alive === true ? "yes" : item.alive === false ? "no" : "unknown",
        hasNsfwTag(item) ? "yes" : "no",
        item.quota || 0,
      ].join(",")
    );
  });

  downloadFile(
    `${lines.join("\n")}\n`,
    `grok2api_tokens_${new Date().toISOString().slice(0, 10)}.csv`,
    "text/csv;charset=utf-8;"
  );
  showToast(`已导出 ${allTokens.length} 个账号`, "success");
}

function openImportModal() {
  const controller = ensureImportController();
  if (controller) controller.openModal("batch");
}

function closeImportModal() {
  const controller = ensureImportController();
  if (controller) controller.closeModal();
}

function addToken() {
  const controller = ensureImportController();
  if (controller) controller.openModal("single");
}

function handleCsvUpload(event) {
  const controller = ensureImportController();
  if (controller) controller.handleCsvUpload(event);
}

function downloadTemplate() {
  const controller = ensureImportController();
  if (controller) controller.downloadTemplate();
}

async function submitImport() {
  const controller = ensureImportController();
  if (controller) await controller.submitImport();
}

window.addEventListener("load", init);

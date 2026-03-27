let apiKey = "";
let pendingConfirmFn = null;

const state = {
  page: 1,
  pageSize: 100,
  data: null,
};

const byId = (id) => document.getElementById(id);

function getAdminModalHelper() {
  return window.AdminModal && typeof window.AdminModal.open === "function"
    ? window.AdminModal
    : null;
}

function openOverlay(id) {
  const helper = getAdminModalHelper();
  if (helper) {
    helper.open(id);
    return;
  }
  const overlay = byId(id);
  if (!overlay) return;
  overlay.classList.remove("hidden");
  requestAnimationFrame(() => overlay.classList.add("is-open"));
}

function closeOverlay(id, onClosed) {
  const helper = getAdminModalHelper();
  if (helper) {
    helper.close(id, { onClosed });
    return;
  }
  const overlay = byId(id);
  if (!overlay) return;
  overlay.classList.remove("is-open");
  setTimeout(() => {
    overlay.classList.add("hidden");
    if (typeof onClosed === "function") onClosed();
  }, 200);
}

function setupAdminModal() {
  const helper = getAdminModalHelper();
  if (!helper) return;
  helper.register("confirm-overlay", { onRequestClose: () => closeConfirm() });
}

function setText(id, value) {
  const node = byId(id);
  if (node) node.textContent = value;
}

function formatTime(timestamp) {
  if (!timestamp) return "-";
  const date = new Date(Number(timestamp));
  if (Number.isNaN(date.getTime())) return "-";
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function maskToken(token) {
  const value = String(token || "");
  if (!value) return "未分配账号";
  if (value.length <= 20) return value;
  return `${value.slice(0, 8)}...${value.slice(-10)}`;
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
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

function setStatus(message) {
  setText("call-logs-status", message || "");
}

function setRefreshLoading(isLoading) {
  const button = byId("refresh-call-logs-btn");
  if (!button) return;
  button.disabled = Boolean(isLoading);
  button.textContent = isLoading ? "刷新中..." : "刷新";
}

function getFilters() {
  return {
    status: (byId("filter-status") && byId("filter-status").value) || "",
    api_type: (byId("filter-api-type") && byId("filter-api-type").value.trim()) || "",
    model: (byId("filter-model") && byId("filter-model").value.trim()) || "",
    account_keyword: (byId("filter-account") && byId("filter-account").value.trim()) || "",
    date_from: (byId("filter-date-from") && byId("filter-date-from").value) || "",
    date_to: (byId("filter-date-to") && byId("filter-date-to").value) || "",
    page: String(state.page),
    page_size: String(state.pageSize),
  };
}

function renderSummary(summary = {}) {
  setText("summary-total", String(summary.total_calls || 0));
  setText("summary-success", String(summary.success_count || 0));
  setText("summary-fail", String(summary.fail_count || 0));
  setText("summary-avg", `${summary.avg_duration_ms || 0} ms`);
}

function renderMigrationStatus(migrationStatus = {}) {
  const node = byId("migration-status");
  if (!node) return;
  const stateValue = String(migrationStatus.state || "");
  if (stateValue === "failed") {
    node.textContent = `历史日志迁移失败：${migrationStatus.message || "请查看服务端日志。"}`;
    return;
  }
  if (stateValue === "completed" && Number(migrationStatus.migrated_count || 0) > 0) {
    node.textContent = `历史日志迁移完成，已导入 ${migrationStatus.migrated_count} 条旧记录。`;
    return;
  }
  if (stateValue === "cleared") {
    node.textContent = "历史旧日志已在清空操作中一并清除。";
    return;
  }
  node.textContent = "";
}

function renderAccounts(accounts = [], summary = {}) {
  const tbody = byId("accounts-table-body");
  if (!tbody) return;

  const totalAccounts = Number(summary.unique_accounts || 0);
  if (!Array.isArray(accounts) || accounts.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="8" class="text-center text-sm text-[var(--accents-4)] py-8">暂无账号聚合数据</td></tr>';
    setText("accounts-count", totalAccounts > 0 ? `共 ${totalAccounts} 个账号` : "");
    return;
  }

  setText("accounts-count", `显示 Top ${accounts.length} / 共 ${totalAccounts} 个账号`);
  tbody.innerHTML = accounts
    .map(
      (item) => `
        <tr>
          <td class="text-left">${escapeHtml(item.email || "-")}</td>
          <td class="text-left font-mono text-xs text-gray-500 call-log-token-cell" title="${escapeHtml(item.token || "")}">${escapeHtml(maskToken(item.token || ""))}</td>
          <td class="text-center">${escapeHtml(item.pool || "-")}</td>
          <td class="text-center">${item.call_count || 0}</td>
          <td class="text-center">${item.success_count || 0}</td>
          <td class="text-center">${item.fail_count || 0}</td>
          <td class="text-center">${item.avg_duration_ms || 0} ms</td>
          <td class="text-center text-xs call-log-time-cell">${formatTime(item.last_called_at)}</td>
        </tr>
      `
    )
    .join("");
}

function renderLogs(items = [], pagination = {}) {
  const tbody = byId("logs-table-body");
  if (!tbody) return;

  if (!Array.isArray(items) || items.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="10" class="text-center text-sm text-[var(--accents-4)] py-8">暂无调用明细</td></tr>';
  } else {
    tbody.innerHTML = items
      .map(
        (item) => `
          <tr>
            <td class="text-left text-xs">${formatTime(item.created_at)}</td>
            <td class="text-center">
              <span class="call-log-status-badge" data-status="${escapeHtml(item.status || "fail")}">
                ${item.status === "success" ? "成功" : "失败"}
              </span>
            </td>
            <td class="text-left text-xs">${escapeHtml(item.api_type || "-")}</td>
            <td class="text-left text-xs">${escapeHtml(item.model || "-")}</td>
            <td class="text-left text-xs">${escapeHtml(item.email || "未分配账号")}</td>
            <td class="text-left font-mono text-xs text-gray-500 call-log-token-cell" title="${escapeHtml(item.token || "")}">${escapeHtml(maskToken(item.token || ""))}</td>
            <td class="text-center text-xs">${escapeHtml(item.pool || "-")}</td>
            <td class="text-center text-xs">${item.duration_ms || 0} ms</td>
            <td class="text-left font-mono text-xs call-log-trace-cell">${escapeHtml(item.trace_id || "-")}</td>
            <td class="text-left text-xs call-log-error-cell">
              <div>${escapeHtml(item.error_code || "-")}</div>
              <div class="call-log-secondary">${escapeHtml(item.error_message || "")}</div>
            </td>
          </tr>
        `
      )
      .join("");
  }

  const totalItems = pagination.total_items || 0;
  const totalPages = pagination.total_pages || 1;
  const currentPage = pagination.page || 1;
  setText("logs-count", `当前筛选共 ${totalItems} 条记录`);
  setText("page-info", `第 ${currentPage} / ${totalPages} 页`);

  const prevButton = byId("page-prev");
  const nextButton = byId("page-next");
  if (prevButton) prevButton.disabled = currentPage <= 1;
  if (nextButton) nextButton.disabled = currentPage >= totalPages;
}

function renderAll(data = {}) {
  state.data = data;
  renderSummary(data.summary || {});
  renderMigrationStatus(data.migration_status || {});
  renderAccounts(data.accounts || [], data.summary || {});
  renderLogs(data.items || [], data.pagination || {});
}

async function loadCallLogs() {
  setStatus("加载中...");
  const params = new URLSearchParams();
  Object.entries(getFilters()).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });

  try {
    const response = await fetch(`/v1/admin/call-logs?${params.toString()}`, {
      headers: buildAuthHeaders(apiKey),
    });
    const data = await readJsonResponse(response);
    if (response.status === 401) {
      logout();
      return;
    }
    if (!response.ok) {
      throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
    }
    renderAll(data || {});
    setStatus("");
  } catch (error) {
    console.error(error);
    setStatus("加载失败");
    showToast(`加载失败：${error.message}`, "error");
  }
}

function applyFilters() {
  state.page = 1;
  loadCallLogs();
}

function resetFilters() {
  ["filter-status", "filter-api-type", "filter-model", "filter-account", "filter-date-from", "filter-date-to"].forEach((id) => {
    const node = byId(id);
    if (node) node.value = "";
  });
  applyFilters();
}

function changePage(offset) {
  const pagination = state.data && state.data.pagination;
  if (!pagination) return;
  const nextPage = (pagination.page || 1) + offset;
  if (nextPage < 1 || nextPage > (pagination.total_pages || 1)) return;
  state.page = nextPage;
  loadCallLogs();
}

function showConfirm(title, message, onConfirm) {
  const overlay = byId("confirm-overlay");
  const titleNode = byId("confirm-title");
  const messageNode = byId("confirm-message");
  if (!overlay || !titleNode || !messageNode) return;
  pendingConfirmFn = onConfirm;
  titleNode.textContent = title;
  messageNode.textContent = message;
  overlay.classList.remove("hidden");
  requestAnimationFrame(() => overlay.classList.add("is-open"));
}

function closeConfirm() {
  const overlay = byId("confirm-overlay");
  if (!overlay) {
    pendingConfirmFn = null;
    return;
  }
  overlay.classList.remove("is-open");
  setTimeout(() => overlay.classList.add("hidden"), 200);
  pendingConfirmFn = null;
}

function confirmAction() {
  const confirmFn = pendingConfirmFn;
  closeConfirm();
  if (typeof confirmFn === "function") confirmFn();
}

function parseFilename(response) {
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/i);
  return match && match[1] ? match[1] : "call-logs.csv";
}

async function exportCallLogs() {
  try {
    const response = await fetch("/v1/admin/call-logs/export", {
      headers: buildAuthHeaders(apiKey),
    });
    if (response.status === 401) {
      logout();
      return;
    }
    if (!response.ok) {
      const data = await readJsonResponse(response);
      throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = parseFilename(response);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    showToast("CSV 导出已开始下载", "success");
  } catch (error) {
    console.error(error);
    showToast(`导出失败：${error.message}`, "error");
  }
}

function clearCallLogs() {
  showConfirm("清空调用日志", "确认清空全部调用日志吗？此操作不可恢复。", async () => {
    try {
      const response = await fetch("/v1/admin/call-logs", {
        method: "DELETE",
        headers: buildAuthHeaders(apiKey),
      });
      const data = await readJsonResponse(response);
      if (response.status === 401) {
        logout();
        return;
      }
      if (!response.ok) {
        throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
      }
      showToast(`已清空 ${data && data.deleted ? data.deleted : 0} 条日志`, "success");
      state.page = 1;
      await loadCallLogs();
    } catch (error) {
      console.error(error);
      showToast(`清空失败：${error.message}`, "error");
    }
  });
}

async function loadCallLogs() {
  setStatus("加载中...");
  setRefreshLoading(true);
  const params = new URLSearchParams();
  Object.entries(getFilters()).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });

  try {
    const response = await fetch(`/v1/admin/call-logs?${params.toString()}`, {
      headers: buildAuthHeaders(apiKey),
    });
    const data = await readJsonResponse(response);
    if (response.status === 401) {
      logout();
      return;
    }
    if (!response.ok) {
      throw new Error((data && (data.detail || data.message)) || `HTTP ${response.status}`);
    }
    renderAll(data || {});
    setStatus("");
  } catch (error) {
    console.error(error);
    setStatus("加载失败");
    showToast(`加载失败：${error.message}`, "error");
  } finally {
    setRefreshLoading(false);
  }
}

function refreshCallLogs() {
  loadCallLogs();
}

function showConfirm(title, message, onConfirm) {
  const overlay = byId("confirm-overlay");
  const titleNode = byId("confirm-title");
  const messageNode = byId("confirm-message");
  if (!overlay || !titleNode || !messageNode) return;
  pendingConfirmFn = onConfirm;
  titleNode.textContent = title;
  messageNode.textContent = message;
  openOverlay("confirm-overlay");
}

function closeConfirm() {
  pendingConfirmFn = null;
  closeOverlay("confirm-overlay");
}

async function init() {
  apiKey = await ensureAdminKey();
  if (apiKey === null) return;
  setupAdminModal();
  await loadCallLogs();
}

window.applyFilters = applyFilters;
window.resetFilters = resetFilters;
window.changePage = changePage;
window.refreshCallLogs = refreshCallLogs;
window.exportCallLogs = exportCallLogs;
window.clearCallLogs = clearCallLogs;
window.closeConfirm = closeConfirm;
window.confirmAction = confirmAction;

window.addEventListener("load", init);

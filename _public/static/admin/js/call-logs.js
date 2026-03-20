let apiKey = "";
let pendingConfirmFn = null;

const state = {
  page: 1,
  pageSize: 50,
  data: null,
};

const byId = (id) => document.getElementById(id);

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

function setDefaultDates() {
  const toNode = byId("filter-date-to");
  const fromNode = byId("filter-date-from");
  if (!toNode || !fromNode || toNode.value || fromNode.value) return;

  const now = new Date();
  const start = new Date(now.getTime() - 6 * 24 * 3600 * 1000);
  toNode.value = now.toISOString().slice(0, 10);
  fromNode.value = start.toISOString().slice(0, 10);
}

function renderSummary(summary = {}) {
  setText("summary-total", String(summary.total_calls || 0));
  setText("summary-success", String(summary.success_count || 0));
  setText("summary-fail", String(summary.fail_count || 0));
  setText("summary-avg", `${summary.avg_duration_ms || 0} ms`);
}

function renderAccounts(accounts = []) {
  const tbody = byId("accounts-table-body");
  if (!tbody) return;

  if (!Array.isArray(accounts) || accounts.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="8" class="text-center text-sm text-[var(--accents-4)] py-8">暂无账号聚合数据</td></tr>';
    setText("accounts-count", "");
    return;
  }

  setText("accounts-count", `共 ${accounts.length} 个账号聚合结果`);
  tbody.innerHTML = accounts
    .map(
      (item) => `
        <tr>
          <td class="text-left">${escapeHtml(item.email || "-")}</td>
          <td class="text-left font-mono text-xs text-gray-500" title="${escapeHtml(item.token || "")}">${escapeHtml(maskToken(item.token || ""))}</td>
          <td class="text-center">${escapeHtml(item.pool || "-")}</td>
          <td class="text-center">${item.call_count || 0}</td>
          <td class="text-center">${item.success_count || 0}</td>
          <td class="text-center">${item.fail_count || 0}</td>
          <td class="text-center">${item.avg_duration_ms || 0} ms</td>
          <td class="text-center text-xs">${formatTime(item.last_called_at)}</td>
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
            <td class="text-left font-mono text-xs text-gray-500" title="${escapeHtml(item.token || "")}">${escapeHtml(maskToken(item.token || ""))}</td>
            <td class="text-center text-xs">${escapeHtml(item.pool || "-")}</td>
            <td class="text-center text-xs">${item.duration_ms || 0} ms</td>
            <td class="text-left font-mono text-xs">${escapeHtml(item.trace_id || "-")}</td>
            <td class="text-left text-xs">
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

  setText("logs-count", `当前筛选共 ${totalItems} 条`);
  setText("page-info", `第 ${currentPage} / ${totalPages} 页`);

  const prevButton = byId("page-prev");
  const nextButton = byId("page-next");
  if (prevButton) prevButton.disabled = currentPage <= 1;
  if (nextButton) nextButton.disabled = currentPage >= totalPages;
}

function renderAll(data = {}) {
  state.data = data;
  renderSummary(data.summary || {});
  renderAccounts(data.accounts || []);
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
  ["filter-status", "filter-api-type", "filter-model", "filter-account"].forEach((id) => {
    const node = byId(id);
    if (node) node.value = "";
  });

  const now = new Date();
  const start = new Date(now.getTime() - 6 * 24 * 3600 * 1000);
  const fromNode = byId("filter-date-from");
  const toNode = byId("filter-date-to");
  if (fromNode) fromNode.value = start.toISOString().slice(0, 10);
  if (toNode) toNode.value = now.toISOString().slice(0, 10);

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

function clearCallLogs() {
  showConfirm("清空调用日志", "确认清空全部调用日志吗？此操作不可恢复。", async () => {
    try {
      const response = await fetch("/v1/admin/call-logs", {
        method: "DELETE",
        headers: buildAuthHeaders(apiKey),
      });
      const data = await readJsonResponse(response);

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

async function init() {
  apiKey = await ensureAdminKey();
  if (apiKey === null) return;

  setDefaultDates();
  await loadCallLogs();
}

window.applyFilters = applyFilters;
window.resetFilters = resetFilters;
window.changePage = changePage;
window.clearCallLogs = clearCallLogs;
window.closeConfirm = closeConfirm;
window.confirmAction = confirmAction;

window.addEventListener("load", init);

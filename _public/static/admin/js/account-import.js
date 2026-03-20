(function (global) {
  const VALID_POOLS = new Set(["ssoBasic", "ssoSuper"]);
  const TRUEISH = new Set(["1", "true", "yes", "on"]);

  function normalizeToken(value) {
    if (value == null) return "";
    return String(value)
      .replace(/[\u2010\u2011\u2012\u2013\u2014\u2212]/g, "-")
      .replace(/[\u00a0\u2007\u202f]/g, " ")
      .replace(/[\u200b\u200c\u200d\ufeff]/g, "")
      .replace(/\s+/g, "")
      .replace(/^sso=/, "");
  }

  function normalizePool(value, fallbackPool) {
    const pool = String(value || "").trim();
    if (VALID_POOLS.has(pool)) return pool;
    return VALID_POOLS.has(fallbackPool) ? fallbackPool : "ssoBasic";
  }

  function parseNsfwFlag(value) {
    return TRUEISH.has(String(value || "").trim().toLowerCase());
  }

  function splitCsvLine(line) {
    const cells = [];
    let current = "";
    let inQuotes = false;

    for (let index = 0; index < line.length; index += 1) {
      const char = line[index];
      const next = line[index + 1];

      if (char === '"') {
        if (inQuotes && next === '"') {
          current += '"';
          index += 1;
        } else {
          inQuotes = !inQuotes;
        }
        continue;
      }

      if (char === "," && !inQuotes) {
        cells.push(current.trim());
        current = "";
        continue;
      }

      current += char;
    }

    cells.push(current.trim());
    return cells;
  }

  function detectColumns(firstRow) {
    const normalized = firstRow.map((cell) => String(cell || "").trim().toLowerCase());
    const hasHeader = normalized.includes("token");

    return {
      hasHeader,
      token: hasHeader ? normalized.indexOf("token") : 0,
      pool: hasHeader ? normalized.indexOf("pool") : 1,
      nsfw: hasHeader ? normalized.indexOf("nsfw") : 2,
      email: hasHeader ? normalized.indexOf("email") : 3,
    };
  }

  function buildEntry({ token, pool = "", nsfwRequested = false, email = "" }) {
    const normalizedToken = normalizeToken(token);
    if (!normalizedToken) return null;

    return {
      token: normalizedToken,
      pool: VALID_POOLS.has(String(pool || "").trim()) ? String(pool).trim() : "",
      nsfwRequested: Boolean(nsfwRequested),
      email: String(email || "").trim(),
    };
  }

  function parseCsvText(csvText) {
    const lines = String(csvText || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);

    if (lines.length === 0) {
      return {
        entries: [],
        totalLines: 0,
        skippedLines: 0,
        hasHeader: false,
      };
    }

    const columns = detectColumns(splitCsvLine(lines[0]));
    const sourceLines = columns.hasHeader ? lines.slice(1) : lines;
    const entries = [];
    let skippedLines = 0;

    for (const line of sourceLines) {
      const cells = splitCsvLine(line);
      const entry = buildEntry({
        token: columns.token >= 0 ? cells[columns.token] : "",
        pool: columns.pool >= 0 ? cells[columns.pool] : "",
        nsfwRequested: columns.nsfw >= 0 ? parseNsfwFlag(cells[columns.nsfw]) : false,
        email: columns.email >= 0 ? cells[columns.email] : "",
      });

      if (!entry) {
        skippedLines += 1;
        continue;
      }

      entries.push(entry);
    }

    return {
      entries,
      totalLines: sourceLines.length,
      skippedLines,
      hasHeader: columns.hasHeader,
    };
  }

  function parseTokenText(text, defaultPool) {
    const lines = String(text || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);

    return lines
      .map((line) => {
        const separatorIndex = line.indexOf(":");
        let pool = defaultPool;
        let token = line;

        if (separatorIndex > 0) {
          const candidatePool = line.slice(0, separatorIndex).trim();
          if (VALID_POOLS.has(candidatePool)) {
            pool = candidatePool;
            token = line.slice(separatorIndex + 1);
          }
        }

        return buildEntry({ token, pool });
      })
      .filter(Boolean);
  }

  function resolveEntryPools(entries, defaultPool) {
    return entries.map((entry) => ({
      ...entry,
      pool: normalizePool(entry.pool, defaultPool),
    }));
  }

  function mergeImportEntries(textEntries, csvEntries) {
    const merged = new Map();

    for (const entry of textEntries) {
      merged.set(entry.token, { ...entry });
    }

    for (const entry of csvEntries) {
      const existing = merged.get(entry.token);
      merged.set(
        entry.token,
        existing
          ? {
              ...existing,
              ...entry,
              nsfwRequested: entry.nsfwRequested,
              email: entry.email,
            }
          : { ...entry }
      );
    }

    return Array.from(merged.values());
  }

  function cloneTokenItem(item) {
    return typeof item === "string" ? { token: item } : { ...item };
  }

  function prepareImportPayload(existingTokens, entries) {
    const payload = {};
    const tokenIndex = new Map();
    let addedCount = 0;
    let existingCount = 0;
    const nsfwTargets = [];
    const scheduledNsfw = new Set();

    for (const [poolName, list] of Object.entries(existingTokens || {})) {
      if (!Array.isArray(list)) continue;
      payload[poolName] = list.map((item) => {
        const cloned = cloneTokenItem(item);
        cloned.token = normalizeToken(cloned.token);
        if (cloned.token && !tokenIndex.has(cloned.token)) {
          tokenIndex.set(cloned.token, { pool: poolName, item: cloned });
        }
        return cloned;
      });
    }

    for (const entry of entries) {
      const token = normalizeToken(entry.token);
      if (!token) continue;

      const targetPool = normalizePool(entry.pool, "ssoBasic");
      const current = tokenIndex.get(token);
      const merged = current ? { ...cloneTokenItem(current.item), token } : { token };
      const hadNsfw = Array.isArray(merged.tags) && merged.tags.includes("nsfw");

      if (current) {
        existingCount += 1;
      } else {
        addedCount += 1;
      }

      if (!payload[targetPool]) payload[targetPool] = [];

      if (current && current.pool !== targetPool && Array.isArray(payload[current.pool])) {
        payload[current.pool] = payload[current.pool].filter(
          (item) => normalizeToken(item.token) !== token
        );
      }

      const targetList = payload[targetPool];
      const targetIndex = targetList.findIndex((item) => normalizeToken(item.token) === token);
      const nextItem =
        targetIndex >= 0
          ? { ...targetList[targetIndex], ...merged, token }
          : { ...merged, token };

      if (targetIndex >= 0) {
        targetList[targetIndex] = nextItem;
      } else {
        targetList.push(nextItem);
      }

      tokenIndex.set(token, { pool: targetPool, item: nextItem });

      if (entry.nsfwRequested && !hadNsfw && !scheduledNsfw.has(token)) {
        scheduledNsfw.add(token);
        nsfwTargets.push(token);
      }
    }

    return {
      payload,
      addedCount,
      existingCount,
      totalCount: entries.length,
      nsfwTargets,
    };
  }

  async function readJsonResponse(response) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  function buildImportSummary(prepared, nsfwSummary) {
    const segments = [
      `导入完成：共处理 ${prepared.totalCount} 个 Token`,
      `新增 ${prepared.addedCount} 个`,
    ];

    if (prepared.existingCount > 0) {
      segments.push(`已存在 ${prepared.existingCount} 个`);
    }

    let message = segments.join("，");

    if (nsfwSummary) {
      message += `；NSFW 成功 ${nsfwSummary.ok}，失败 ${nsfwSummary.fail}`;
    }

    return message;
  }

  function isCsvFile(file) {
    if (!file) return false;
    const name = String(file.name || "").trim().toLowerCase();
    const type = String(file.type || "").trim().toLowerCase();

    return (
      name.endsWith(".csv") ||
      type === "text/csv" ||
      type === "application/csv" ||
      type === "application/vnd.ms-excel"
    );
  }

  function pickCsvFile(files) {
    return Array.from(files || []).find((file) => isCsvFile(file)) || null;
  }

  function createController(options) {
    const state = {
      csvEntries: [],
      csvMeta: { totalLines: 0, skippedLines: 0 },
      busy: false,
      batchEventSource: null,
      dragDepth: 0,
      dropOverlayActive: false,
    };

    const byId = options.byId || ((id) => document.getElementById(id));
    const showToast = options.showToast || (() => {});

    function getImportModal() {
      return byId("import-modal");
    }

    function isModalOpen() {
      const modal = getImportModal();
      return Boolean(modal && !modal.classList.contains("hidden"));
    }

    function hasFilePayload(event) {
      const dataTransfer = event && event.dataTransfer;
      if (!dataTransfer) return false;
      if (dataTransfer.types && Array.from(dataTransfer.types).includes("Files")) return true;
      return Boolean(dataTransfer.files && dataTransfer.files.length);
    }

    function renderCsvState() {
      const container = byId("import-csv-state");
      if (!container) return;

      if (state.csvEntries.length === 0) {
        container.textContent = "";
        container.classList.add("hidden");
        return;
      }

      const nsfwCount = state.csvEntries.filter((entry) => entry.nsfwRequested).length;
      const parts = [`已加载 CSV：有效 ${state.csvEntries.length} 条`];

      if (nsfwCount > 0) {
        parts.push(`标记 NSFW ${nsfwCount} 条`);
      }

      if (state.csvMeta.skippedLines > 0) {
        parts.push(`跳过 ${state.csvMeta.skippedLines} 行`);
      }

      container.textContent = parts.join("，");
      container.classList.remove("hidden");
    }

    function renderProgress(message, tone = "info") {
      const container = byId("import-progress");
      if (!container) return;

      if (!message) {
        container.textContent = "";
        container.dataset.tone = "info";
        container.classList.add("hidden");
        return;
      }

      container.textContent = message;
      container.dataset.tone = tone;
      container.classList.remove("hidden");
    }

    function setDropOverlay(active) {
      state.dropOverlayActive = active;
      const overlay = byId("import-drop-overlay");
      if (!overlay) return;
      overlay.classList.toggle("is-active", active);
    }

    function setBusy(isBusy) {
      state.busy = isBusy;

      ["import-pool", "import-text", "import-csv", "import-submit-btn", "import-cancel-btn"].forEach(
        (id) => {
          const element = byId(id);
          if (element) element.disabled = isBusy;
        }
      );
    }

    function closeStream() {
      if (state.batchEventSource && global.BatchSSE) {
        global.BatchSSE.close(state.batchEventSource);
      }
      state.batchEventSource = null;
    }

    function clearCsvState() {
      state.csvEntries = [];
      state.csvMeta = { totalLines: 0, skippedLines: 0 };
      renderCsvState();

      const csvInput = byId("import-csv");
      if (csvInput) csvInput.value = "";
    }

    function resetState() {
      closeStream();
      clearCsvState();
      state.dragDepth = 0;
      setDropOverlay(false);
      setBusy(false);
      renderProgress("");
    }

    function openModal(mode = "batch") {
      resetState();

      const textInput = byId("import-text");
      if (textInput) {
        textInput.value = "";
        textInput.placeholder =
          mode === "single" ? "输入单个 Token..." : "粘贴 Token，一行一个...";
      }

      const modal = getImportModal();
      if (!modal) return;

      modal.classList.remove("hidden");
      requestAnimationFrame(() => modal.classList.add("is-open"));
    }

    function closeModal(force = false) {
      if (state.busy && !force) return;

      const modal = getImportModal();
      if (!modal) return;

      modal.classList.remove("is-open");
      setTimeout(() => modal.classList.add("hidden"), 200);
      resetState();
    }

    function downloadTemplate() {
      const csv = [
        "token,pool,nsfw,email",
        "your_token_here,ssoBasic,yes,user1@example.com",
        "your_other_token,ssoSuper,no,user2@example.com",
      ].join("\n");
      const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
      const link = document.createElement("a");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = "token_import_template.csv";
      link.click();
      URL.revokeObjectURL(url);
    }

    function loadCsvFile(file, source = "upload") {
      if (!file) return Promise.resolve(false);

      return new Promise((resolve) => {
        const reader = new FileReader();

        reader.onload = (loadEvent) => {
          const parsed = parseCsvText(loadEvent.target && loadEvent.target.result);
          state.csvEntries = parsed.entries;
          state.csvMeta = {
            totalLines: parsed.totalLines,
            skippedLines: parsed.skippedLines,
          };
          renderCsvState();
          showToast(
            `${source === "drag" ? "拖拽" : "上传"}已识别 ${file.name || "CSV"}，有效 ${parsed.entries.length} 条`,
            "success"
          );
          resolve(true);
        };

        reader.onerror = () => {
          showToast("CSV 读取失败", "error");
          resolve(false);
        };

        reader.readAsText(file);
      });
    }

    async function handleCsvUpload(event) {
      if (state.busy) return;

      const file = pickCsvFile(event.target && event.target.files);
      if (!file) {
        clearCsvState();
        if (event.target && event.target.files && event.target.files.length > 0) {
          showToast("仅支持上传 CSV 文件", "warning");
        }
        return;
      }

      await loadCsvFile(file, "upload");
    }

    function handleGlobalDragEnter(event) {
      if (!hasFilePayload(event) || state.busy) return;

      event.preventDefault();
      state.dragDepth += 1;
      setDropOverlay(true);
    }

    function handleGlobalDragOver(event) {
      if (!hasFilePayload(event) || state.busy) return;

      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      if (!state.dropOverlayActive) setDropOverlay(true);
    }

    function handleGlobalDragLeave(event) {
      if (!hasFilePayload(event) || state.busy) return;

      event.preventDefault();
      state.dragDepth = Math.max(0, state.dragDepth - 1);
      if (state.dragDepth === 0) setDropOverlay(false);
    }

    function handleGlobalDragEnd() {
      state.dragDepth = 0;
      setDropOverlay(false);
    }

    async function handleGlobalDrop(event) {
      if (!hasFilePayload(event)) return;

      event.preventDefault();
      state.dragDepth = 0;
      setDropOverlay(false);

      if (state.busy) {
        showToast("导入进行中，请稍候", "warning");
        return;
      }

      const file = pickCsvFile(event.dataTransfer && event.dataTransfer.files);
      if (!file) {
        showToast("仅支持拖拽 CSV 文件", "warning");
        return;
      }

      if (!isModalOpen()) {
        openModal("batch");
      }

      await loadCsvFile(file, "drag");
    }

    function bindGlobalDropEvents() {
      global.addEventListener("dragenter", handleGlobalDragEnter);
      global.addEventListener("dragover", handleGlobalDragOver);
      global.addEventListener("dragleave", handleGlobalDragLeave);
      global.addEventListener("dragend", handleGlobalDragEnd);
      global.addEventListener("drop", handleGlobalDrop);
    }

    async function runNsfwBatch(prepared) {
      if (!global.BatchSSE) {
        throw new Error("Batch SSE is unavailable");
      }

      const response = await fetch("/v1/admin/tokens/nsfw/enable/async", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...options.getAuthHeaders(),
        },
        body: JSON.stringify({ tokens: prepared.nsfwTargets }),
      });
      const data = await readJsonResponse(response);

      if (!response.ok) {
        throw new Error(
          (data && (data.detail || data.message)) || `HTTP ${response.status}`
        );
      }

      if (!data || data.status !== "success" || !data.task_id) {
        throw new Error("未返回有效的 NSFW 任务信息");
      }

      renderProgress(
        `Token 已导入，正在为 ${prepared.nsfwTargets.length} 个账号开启 NSFW...`,
        "info"
      );

      return new Promise((resolve, reject) => {
        const finish = (handler, value) => {
          closeStream();
          handler(value);
        };

        state.batchEventSource = global.BatchSSE.open(data.task_id, options.getApiKey(), {
          onMessage(message) {
            if (message.type === "snapshot" || message.type === "progress") {
              const total = Number.isFinite(message.total)
                ? message.total
                : prepared.nsfwTargets.length;
              const processed = Number.isFinite(message.processed) ? message.processed : 0;
              const ok = Number.isFinite(message.ok) ? message.ok : 0;
              const fail = Number.isFinite(message.fail) ? message.fail : 0;

              renderProgress(
                `Token 已导入，正在开启 NSFW ${processed}/${total}（成功 ${ok}，失败 ${fail}）...`,
                "info"
              );
              return;
            }

            if (message.type === "done") {
              const summary =
                message.result && message.result.summary
                  ? message.result.summary
                  : { ok: 0, fail: 0, total: prepared.nsfwTargets.length };
              finish(resolve, summary);
              return;
            }

            if (message.type === "cancelled") {
              finish(reject, new Error("NSFW 任务已取消"));
              return;
            }

            if (message.type === "error") {
              finish(reject, new Error(message.error || "NSFW 任务失败"));
            }
          },
          onError() {
            finish(reject, new Error("NSFW 任务连接中断"));
          },
        });
      });
    }

    async function submitImport() {
      if (state.busy) return;

      const defaultPool = normalizePool(
        byId("import-pool") && byId("import-pool").value,
        "ssoBasic"
      );
      const textEntries = parseTokenText(
        byId("import-text") && byId("import-text").value,
        defaultPool
      );
      const csvEntries = resolveEntryPools(state.csvEntries, defaultPool);
      const entries = mergeImportEntries(textEntries, csvEntries);

      if (entries.length === 0) {
        showToast("请输入 Token 或上传 CSV", "error");
        return;
      }

      setBusy(true);
      renderProgress(`正在导入 ${entries.length} 个 Token...`, "info");

      try {
        const tokensResponse = await fetch("/v1/admin/tokens", {
          headers: options.getAuthHeaders(),
        });
        const tokensData = await readJsonResponse(tokensResponse);

        if (!tokensResponse.ok) {
          throw new Error(
            (tokensData && (tokensData.detail || tokensData.message)) ||
              `HTTP ${tokensResponse.status}`
          );
        }

        const prepared = prepareImportPayload(tokensData && tokensData.tokens, entries);
        const saveResponse = await fetch("/v1/admin/tokens", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...options.getAuthHeaders(),
          },
          body: JSON.stringify(prepared.payload),
        });
        const saveData = await readJsonResponse(saveResponse);

        if (!saveResponse.ok) {
          throw new Error(
            (saveData && (saveData.detail || saveData.message)) ||
              `HTTP ${saveResponse.status}`
          );
        }

        if (prepared.nsfwTargets.length === 0) {
          await options.onReload();
          closeModal(true);
          showToast(buildImportSummary(prepared), "success");
          return;
        }

        try {
          const nsfwSummary = await runNsfwBatch(prepared);
          await options.onReload();
          closeModal(true);
          showToast(
            buildImportSummary(prepared, nsfwSummary),
            nsfwSummary.fail > 0 ? "warning" : "success"
          );
        } catch (error) {
          await options.onReload();
          closeModal(true);
          showToast(`Token 已导入，但 NSFW 处理失败：${error.message}`, "warning");
        }
      } catch (error) {
        console.error(error);
        setBusy(false);
        renderProgress(`导入失败：${error.message}`, "error");
        showToast(`导入失败：${error.message}`, "error");
      }
    }

    bindGlobalDropEvents();

    return {
      openModal,
      closeModal,
      handleCsvUpload,
      downloadTemplate,
      submitImport,
    };
  }

  const api = {
    normalizeToken,
    parseNsfwFlag,
    parseCsvText,
    parseTokenText,
    resolveEntryPools,
    mergeImportEntries,
    prepareImportPayload,
    isCsvFile,
    pickCsvFile,
    createController,
  };

  global.AccountImport = api;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : window);

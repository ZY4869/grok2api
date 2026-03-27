(function (global) {
  const registry = new Map();
  const openStack = [];

  function byId(id) {
    return document.getElementById(id);
  }

  function resolveOverlay(target) {
    if (!target) return null;
    if (typeof target === "string") return byId(target);
    return target.nodeType === 1 ? target : null;
  }

  function ensureMounted(overlay) {
    if (!overlay || !document.body || overlay.parentElement === document.body) return;
    document.body.appendChild(overlay);
  }

  function syncBodyLock() {
    document.body.classList.toggle("modal-open", openStack.length > 0);
  }

  function removeFromStack(id) {
    const index = openStack.lastIndexOf(id);
    if (index >= 0) openStack.splice(index, 1);
  }

  function requestClose(target) {
    const overlay = resolveOverlay(target);
    if (!overlay) return;
    const state = registry.get(overlay.id);
    if (state && typeof state.onRequestClose === "function") {
      state.onRequestClose();
      return;
    }
    close(overlay);
  }

  function register(target, options) {
    const overlay = resolveOverlay(target);
    if (!overlay || !overlay.id) return null;
    ensureMounted(overlay);

    const current = registry.get(overlay.id) || { overlay };
    registry.set(overlay.id, {
      overlay,
      onRequestClose:
        options && typeof options.onRequestClose === "function"
          ? options.onRequestClose
          : current.onRequestClose || null,
    });

    if (overlay.dataset.adminModalBound !== "1") {
      overlay.dataset.adminModalBound = "1";
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) {
          requestClose(overlay);
        }
      });
    }

    return overlay;
  }

  function mountAll(scope) {
    const root = scope || document;
    root.querySelectorAll(".modal-overlay").forEach((overlay) => {
      register(overlay);
    });
  }

  function open(target) {
    const overlay = register(target);
    if (!overlay) return null;
    removeFromStack(overlay.id);
    openStack.push(overlay.id);
    overlay.classList.remove("hidden");
    requestAnimationFrame(() => {
      overlay.classList.add("is-open");
    });
    syncBodyLock();
    return overlay;
  }

  function close(target, options) {
    const overlay = resolveOverlay(target);
    if (!overlay) return;

    removeFromStack(overlay.id);
    overlay.classList.remove("is-open");

    const onClosed = options && typeof options.onClosed === "function" ? options.onClosed : null;
    setTimeout(() => {
      overlay.classList.add("hidden");
      syncBodyLock();
      if (onClosed) onClosed();
    }, 200);
  }

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || openStack.length === 0) return;
    requestClose(openStack[openStack.length - 1]);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => mountAll(document), {
      once: true,
    });
  } else {
    mountAll(document);
  }

  global.AdminModal = {
    register,
    mountAll,
    open,
    close,
    requestClose,
  };
})(typeof globalThis !== "undefined" ? globalThis : window);

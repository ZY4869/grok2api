async function loadAdminHeader() {
  const container = document.getElementById("app-header");
  if (!container) return;

  try {
    const response = await fetch("/static/common/html/header.html?v=1.7.3");
    if (!response.ok) return;

    container.innerHTML = await response.text();
    const path = window.location.pathname;
    const links = container.querySelectorAll("a[data-nav]");

    links.forEach((link) => {
      const target = link.getAttribute("data-nav") || "";
      if (!target || !path.startsWith(target)) return;

      link.classList.add("active");
      const group = link.closest(".nav-group");
      if (!group) return;

      const trigger = group.querySelector(".nav-group-trigger");
      if (trigger) {
        trigger.classList.add("active");
      }
    });

    if (window.I18n) {
      I18n.applyToDOM(container);
      const toggle = container.querySelector("#lang-toggle");
      if (toggle) {
        toggle.textContent = I18n.getLang() === "zh" ? "EN" : "中";
      }
    }

    if (typeof updateStorageModeButton === "function") {
      updateStorageModeButton();
    }
  } catch (error) {
    // Keep header failures silent so page content can still render.
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", loadAdminHeader);
} else {
  loadAdminHeader();
}

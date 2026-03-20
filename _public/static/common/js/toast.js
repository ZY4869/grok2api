function showToast(message, type = 'success') {
  // Ensure container exists
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  const variants = {
    success: {
      className: 'toast-success',
      iconSvg: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`,
    },
    error: {
      className: 'toast-error',
      iconSvg: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`,
    },
    warning: {
      className: 'toast-warning',
      iconSvg: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>`,
    },
    info: {
      className: 'toast-info',
      iconSvg: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>`,
    },
  };

  const variant = variants[type] || variants.error;

  toast.className = `toast ${variant.className}`;

  // Basic HTML escaping for message
  const escapedMessage = message
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");

  toast.innerHTML = `
        <div class="toast-icon">
          ${variant.iconSvg}
        </div>
        <div class="toast-content">${escapedMessage}</div>
      `;

  container.appendChild(toast);

  // Remove after 3 seconds
  setTimeout(() => {
    toast.classList.add('out');
    toast.addEventListener('animationend', () => {
      if (toast.parentElement) {
        toast.parentElement.removeChild(toast);
      }
    });
  }, 3000);
}

(function showRateLimitNoticeOnce() {
  const noticeKey = 'grok2api_rate_limits_notice_v1';
  const translate = (key, fallback) => {
    if (typeof t !== 'function') return fallback;
    const value = t(key);
    return value === key ? fallback : value;
  };
  const noticeText = translate(
    'common.rateLimitNotice',
    'GROK 官方网页更新后未真实暴露 rate-limits 接口，导致无法准确计算 Token 剩余，请耐心等待官方接口上线，目前自动刷新后会更新为 8 次'
  );
  const path = window.location.pathname || '';

  if (!path.startsWith('/admin') || path.startsWith('/admin/login')) {
    return;
  }

  try {
    if (localStorage.getItem(noticeKey)) {
      return;
    }
  } catch (e) {
    // If storage is blocked, keep showing dialog.
  }

  const show = () => {
    const backdrop = document.createElement('div');
    backdrop.className = 'notice-dialog-backdrop';

    const dialog = document.createElement('div');
    dialog.className = 'notice-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');

    const title = document.createElement('div');
    title.className = 'notice-dialog-title';
    title.textContent = translate('common.notice', '提示');

    const content = document.createElement('div');
    content.className = 'notice-dialog-content';
    content.textContent = noticeText;

    const actions = document.createElement('div');
    actions.className = 'notice-dialog-actions';

    const confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = 'notice-dialog-confirm';
    confirmBtn.textContent = translate('common.gotIt', '我知道了');

    actions.appendChild(confirmBtn);
    dialog.appendChild(title);
    dialog.appendChild(content);
    dialog.appendChild(actions);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);

    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    confirmBtn.addEventListener('click', () => {
      try {
        localStorage.setItem(noticeKey, '1');
      } catch (e) {
        // ignore
      }
      document.body.style.overflow = prevOverflow;
      backdrop.remove();
    });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', show);
  } else {
    show();
  }
})();

(function (global) {
  const TIER_LABELS = {
    SUBSCRIPTION_TIER_INVALID: "Free",
    SUBSCRIPTION_TIER_X_BASIC: "Basic",
    SUBSCRIPTION_TIER_X_PREMIUM: "Premium",
    SUBSCRIPTION_TIER_X_PREMIUM_PLUS: "PremiumPlus",
    SUBSCRIPTION_TIER_GROK_PRO: "SuperGrok",
    SUBSCRIPTION_TIER_SUPER_GROK_PRO: "SuperGrokPro",
  };

  const SUPER_TIERS = new Set([
    "SUBSCRIPTION_TIER_GROK_PRO",
    "SUBSCRIPTION_TIER_SUPER_GROK_PRO",
  ]);

  const PREMIUM_TIERS = new Set([
    "SUBSCRIPTION_TIER_X_PREMIUM",
    "SUBSCRIPTION_TIER_X_PREMIUM_PLUS",
  ]);

  function formatTimestamp(timestamp) {
    if (!timestamp) return "";
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return "";

    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
      date.getHours()
    )}:${pad(date.getMinutes())}`;
  }

  function getTierLabel(tier, fallbackName) {
    if (fallbackName) return String(fallbackName);
    if (!tier) return "未查询";
    return TIER_LABELS[tier] || String(tier).replace(/^SUBSCRIPTION_TIER_/, "");
  }

  function toFiniteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function getBadgeClass(tier, hasError, hasData) {
    if (hasError && !hasData) return "badge-red";
    if (!hasData) return "badge-gray";
    if (SUPER_TIERS.has(tier)) return "badge-purple";
    if (PREMIUM_TIERS.has(tier)) return "badge-orange";
    if (tier === "SUBSCRIPTION_TIER_X_BASIC") return "badge-green";
    return "badge-gray";
  }

  function formatRateLimitSummary(modelName, limit) {
    if (!limit || typeof limit !== "object") return `${modelName}: -`;
    if (limit.error) return `${modelName}: 刷新失败`;

    const remaining = toFiniteNumber(limit.remainingTokens ?? limit.remainingQueries);
    const total = toFiniteNumber(limit.totalTokens ?? limit.totalQueries);
    const waitTimeSeconds = toFiniteNumber(limit.waitTimeSeconds);

    if (remaining !== null && total !== null) {
      return `${modelName}: ${remaining}/${total}`;
    }
    if (remaining !== null) {
      return `${modelName}: ${remaining}`;
    }
    if (waitTimeSeconds !== null && waitTimeSeconds > 0) {
      return `${modelName}: ${waitTimeSeconds}s 后恢复`;
    }
    return `${modelName}: -`;
  }

  function buildTitle(item, label, error) {
    const quota =
      item && item.real_quota && typeof item.real_quota === "object" ? item.real_quota : null;
    const lines = [];

    if (label) {
      lines.push(`真实档位: ${label}`);
    }

    const activeSubscriptions = Array.isArray(quota && quota.active_subscriptions)
      ? quota.active_subscriptions
      : [];
    if (activeSubscriptions.length > 0) {
      lines.push(
        `有效订阅: ${activeSubscriptions
          .map((subscription) => {
            const tierLabel = getTierLabel(subscription.tier, subscription.tier_name);
            const status = String(subscription.status || "").replace(
              /^SUBSCRIPTION_STATUS_/,
              ""
            );
            return `${tierLabel} (${status || "UNKNOWN"})`;
          })
          .join(", ")}`
      );
    }

    const rateLimits =
      quota && quota.rate_limits && typeof quota.rate_limits === "object"
        ? quota.rate_limits
        : {};
    Object.keys(rateLimits).forEach((modelName) => {
      lines.push(formatRateLimitSummary(modelName, rateLimits[modelName]));
    });

    if (error) {
      lines.push(`错误: ${error}`);
    }

    if (item && item.last_real_quota_check_at) {
      lines.push(`更新时间: ${formatTimestamp(item.last_real_quota_check_at)}`);
    }

    return lines.join("\n");
  }

  function getRealQuotaState(item) {
    const quota =
      item && item.real_quota && typeof item.real_quota === "object" ? item.real_quota : null;
    const hasData = Boolean(quota || item.real_tier || item.real_tier_name);
    const tier = item.real_tier || (quota && quota.subscription_tier) || "";
    const rateLimits =
      quota && quota.rate_limits && typeof quota.rate_limits === "object"
        ? quota.rate_limits
        : {};
    const modelNames = Object.keys(rateLimits);
    const hasLiveQuota = modelNames.some((modelName) => {
      const payload = rateLimits[modelName];
      return payload && typeof payload === "object" && !payload.error;
    });
    const partialErrors =
      quota && Array.isArray(quota.partial_errors) ? quota.partial_errors : [];
    const backendError = item.last_real_quota_error || "";
    const error = backendError || (!hasLiveQuota ? partialErrors.join("；") : "");
    const summary = modelNames.length
      ? modelNames
          .map((modelName) => formatRateLimitSummary(modelName, rateLimits[modelName]))
          .join(" | ")
      : error
        ? "本次刷新未获取到实时额度"
        : "点击刷新真实额度";

    const label = !hasData && error
      ? "刷新失败"
      : hasData
        ? getTierLabel(tier, item.real_tier_name || (quota && quota.subscription_name))
        : "未查询";

    return {
      label,
      badgeClass: getBadgeClass(tier, Boolean(error), hasData),
      summary,
      meta: item.last_real_quota_check_at
        ? `更新 ${formatTimestamp(item.last_real_quota_check_at)}`
        : "",
      error,
      title: buildTitle(item, label, error),
    };
  }

  const api = {
    formatRateLimitSummary,
    formatTimestamp,
    getRealQuotaState,
    getTierLabel,
  };

  global.AccountRealQuota = api;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : window);

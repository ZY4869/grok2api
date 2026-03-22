(function (global) {
  const TIER_LABELS = {
    SUBSCRIPTION_TIER_INVALID: "Free",
    SUBSCRIPTION_TIER_X_BASIC: "Basic",
    SUBSCRIPTION_TIER_X_PREMIUM: "Premium",
    SUBSCRIPTION_TIER_X_PREMIUM_PLUS: "PremiumPlus",
    SUBSCRIPTION_TIER_GROK_PRO: "SuperGrok",
    SUBSCRIPTION_TIER_SUPER_GROK_PRO: "SuperGrokPro",
  };

  const ITEM_ROWS = [
    [
      { modelName: "grok-3", markerText: "3", tooltipLabel: "3额度", tone: "text-3" },
      { modelName: "grok-4", markerText: "4", tooltipLabel: "4额度", tone: "text-4" },
    ],
    [
      {
        modelName: "grok-imagine-1.0",
        tooltipLabel: "图片额度",
        tone: "image",
        symbol: "image",
      },
      {
        modelName: "grok-imagine-1.0-video",
        tooltipLabel: "视频额度",
        tone: "video",
        symbol: "video",
      },
    ],
  ];

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

  function describeRateLimit(limit) {
    if (!limit || typeof limit !== "object") {
      return { value: "-", detail: "", status: "empty", rawError: "", sourceModelName: "" };
    }
    if (limit.error) {
      return {
        value: "刷新失败",
        detail: "",
        status: "error",
        rawError: String(limit.error),
        sourceModelName: String(limit.sourceModelName || ""),
      };
    }

    const remaining = toFiniteNumber(limit.remainingTokens ?? limit.remainingQueries);
    const total = toFiniteNumber(limit.totalTokens ?? limit.totalQueries);
    const waitTimeSeconds = toFiniteNumber(limit.waitTimeSeconds);

    if (remaining !== null && total !== null) {
      return {
        value: `${remaining}/${total}`,
        detail: "",
        status: "ready",
        rawError: "",
        sourceModelName: String(limit.sourceModelName || ""),
      };
    }
    if (remaining !== null) {
      return {
        value: `${remaining}`,
        detail: "",
        status: "ready",
        rawError: "",
        sourceModelName: String(limit.sourceModelName || ""),
      };
    }
    if (waitTimeSeconds !== null && waitTimeSeconds > 0) {
      return {
        value: `${waitTimeSeconds}s`,
        detail: "后恢复",
        status: "wait",
        rawError: "",
        sourceModelName: String(limit.sourceModelName || ""),
      };
    }

    return {
      value: "-",
      detail: "",
      status: "empty",
      rawError: "",
      sourceModelName: String(limit.sourceModelName || ""),
    };
  }

  function buildRows(rateLimits) {
    return ITEM_ROWS.map((row) =>
      row.map((config) => {
        const valueState = describeRateLimit(rateLimits[config.modelName]);
        return {
          key: config.modelName,
          modelName: config.modelName,
          markerText: config.markerText || "",
          tooltipLabel: config.tooltipLabel || "",
          tone: config.tone,
          symbol: config.symbol || "",
          value: valueState.value,
          detail: valueState.detail,
          status: valueState.status,
          rawError: valueState.rawError,
          sourceModelName: valueState.sourceModelName,
        };
      })
    );
  }

  function formatItemTitle(item) {
    const parts = [`${item.tooltipLabel}: ${item.value}${item.detail ? ` ${item.detail}` : ""}`];
    if (item.sourceModelName && item.sourceModelName !== item.modelName) {
      parts.push(`来源 ${item.sourceModelName}`);
    }
    if (item.rawError) {
      parts.push(`错误 ${item.rawError}`);
    }
    return parts.join(" | ");
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
    buildRows(rateLimits)
      .flat()
      .forEach((quotaItem) => {
        lines.push(formatItemTitle(quotaItem));
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
    const note = !hasData && !error
      ? "点击刷新真实额度"
      : !hasLiveQuota && error
        ? "本次刷新未获取到实时额度"
        : "";
    const label = !hasData && error
      ? "刷新失败"
      : hasData
        ? getTierLabel(tier, item.real_tier_name || (quota && quota.subscription_name))
        : "未查询";

    return {
      label,
      badgeClass: getBadgeClass(tier, Boolean(error), hasData),
      rows: buildRows(rateLimits),
      note,
      meta: item.last_real_quota_check_at
        ? `更新 ${formatTimestamp(item.last_real_quota_check_at)}`
        : "",
      error,
      title: buildTitle(item, label, error),
    };
  }

  const api = {
    formatTimestamp,
    getRealQuotaState,
    getTierLabel,
  };

  global.AccountRealQuota = api;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : window);

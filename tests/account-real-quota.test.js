const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const realQuota = require(path.join(
  __dirname,
  "..",
  "_public",
  "static",
  "admin",
  "js",
  "account-real-quota.js"
));

test("getRealQuotaState renders tier and live rate limit summary", () => {
  const state = realQuota.getRealQuotaState({
    real_tier: "SUBSCRIPTION_TIER_GROK_PRO",
    real_quota: {
      subscription_name: "SuperGrok",
      active_subscriptions: [
        {
          tier: "SUBSCRIPTION_TIER_GROK_PRO",
          tier_name: "SuperGrok",
          status: "SUBSCRIPTION_STATUS_ACTIVE",
        },
      ],
      rate_limits: {
        "grok-3": { remainingTokens: 120, totalTokens: 1000 },
        "grok-4": { remainingQueries: 6, totalQueries: 10 },
      },
    },
    last_real_quota_check_at: 1711111111111,
    last_real_quota_error: "",
  });

  assert.equal(state.label, "SuperGrok");
  assert.equal(state.badgeClass, "badge-purple");
  assert.match(state.summary, /grok-3: 120\/1000/);
  assert.match(state.summary, /grok-4: 6\/10/);
  assert.match(state.title, /真实档位: SuperGrok/);
  assert.match(state.title, /有效订阅:/);
});

test("getRealQuotaState surfaces refresh failure without prior data", () => {
  const state = realQuota.getRealQuotaState({
    real_tier: "",
    real_quota: null,
    last_real_quota_check_at: 0,
    last_real_quota_error: "No live model quota returned",
  });

  assert.equal(state.label, "刷新失败");
  assert.equal(state.badgeClass, "badge-red");
  assert.equal(state.summary, "本次刷新未获取到实时额度");
  assert.match(state.title, /错误:/);
});

test("getRealQuotaState hides partial refresh warnings when live quota exists", () => {
  const state = realQuota.getRealQuotaState({
    real_tier: "SUBSCRIPTION_TIER_GROK_PRO",
    real_quota: {
      subscription_name: "SuperGrok",
      partial_errors: ["refresh-subscription: Request failed, 501"],
      rate_limits: {
        "grok-3": { remainingTokens: 88, totalTokens: 1000 },
      },
    },
    last_real_quota_check_at: 1711111111111,
    last_real_quota_error: "",
  });

  assert.equal(state.label, "SuperGrok");
  assert.equal(state.error, "");
  assert.match(state.summary, /grok-3: 88\/1000/);
});

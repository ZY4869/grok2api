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

test("getRealQuotaState renders two model rows in default view", () => {
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
        "grok-imagine-1.0": { remainingQueries: 4, totalQueries: 12 },
        "grok-imagine-1.0-video": { remainingQueries: 2, totalQueries: 5 },
      },
    },
    last_real_quota_check_at: 1711111111111,
    last_real_quota_error: "",
  }, { displayMode: "model" });

  assert.equal(state.displayMode, "model");
  assert.equal(state.label, "SuperGrok");
  assert.equal(state.badgeClass, "badge-purple");
  assert.equal(state.rows.length, 2);
  assert.equal(state.rows[0][0].label, "3额度");
  assert.equal(state.rows[0][0].value, "120/1000");
  assert.equal(state.rows[0][1].label, "4额度");
  assert.equal(state.rows[0][1].value, "6/10");
  assert.equal(state.rows[1][0].symbol, "image");
  assert.equal(state.rows[1][0].value, "4/12");
  assert.equal(state.rows[1][1].symbol, "video");
  assert.equal(state.rows[1][1].value, "2/5");
  assert.equal(state.modeHint, "");
  assert.match(state.title, /真实档位: SuperGrok/);
  assert.match(state.title, /3额度: 120\/1000/);
  assert.match(state.title, /视频额度: 2\/5/);
  assert.match(state.title, /有效订阅:/);
});

test("getRealQuotaState surfaces refresh failure without prior data", () => {
  const state = realQuota.getRealQuotaState({
    real_tier: "",
    real_quota: null,
    last_real_quota_check_at: 0,
    last_real_quota_error: "No live model quota returned",
  }, { displayMode: "model" });

  assert.equal(state.label, "刷新失败");
  assert.equal(state.badgeClass, "badge-red");
  assert.equal(state.note, "本次刷新未获取到实时额度");
  assert.equal(state.rows[0][0].value, "-");
  assert.equal(state.rows[1][0].symbol, "image");
  assert.match(state.title, /错误:/);
});

test("getRealQuotaState supports shortcut mode and wait state", () => {
  const state = realQuota.getRealQuotaState({
    real_tier: "SUBSCRIPTION_TIER_GROK_PRO",
    real_quota: {
      subscription_name: "SuperGrok",
      rate_limits: {
        "grok-3": { remainingTokens: 88, totalTokens: 1000 },
        "grok-4": { waitTimeSeconds: 45 },
        "grok-imagine-1.0": { remainingQueries: 3, totalQueries: 12 },
      },
    },
    last_real_quota_check_at: 1711111111111,
    last_real_quota_error: "",
  }, { displayMode: "shortcut" });

  assert.equal(state.displayMode, "shortcut");
  assert.equal(state.label, "SuperGrok");
  assert.equal(state.rows[0][0].label, "3 Fast额度");
  assert.equal(state.rows[0][0].value, "88/1000");
  assert.equal(state.rows[0][1].label, "4 Expert/Heavy额度");
  assert.equal(state.rows[0][1].value, "45s");
  assert.equal(state.rows[0][1].detail, "后恢复");
  assert.equal(state.rows[0][1].status, "wait");
  assert.equal(state.rows[1][0].symbol, "image");
  assert.equal(state.rows[1][0].value, "3/12");
  assert.match(state.modeHint, /快捷模式最终消耗对应底层模型额度/);
  assert.match(state.title, /3 Fast额度: 88\/1000/);
});

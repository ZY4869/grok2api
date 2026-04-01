const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

global.window = {};
global.t = (key) => key;

const tokenUi = require(path.join(
  __dirname,
  "..",
  "_public",
  "static",
  "admin",
  "js",
  "token.js"
));

test("getDefaultQuotaForPool gives ssoHeavy a compatibility-only local quota", () => {
  assert.equal(tokenUi.getDefaultQuotaForPool("ssoBasic"), 80);
  assert.equal(tokenUi.getDefaultQuotaForPool("ssoSuper"), 140);
  assert.equal(tokenUi.getDefaultQuotaForPool("ssoHeavy"), 0);
});

test("buildQuotaStats excludes ssoHeavy from local quota totals", () => {
  const stats = tokenUi.buildQuotaStats(
    [
      { pool: "ssoBasic", status: "active", quota: 80, consumed: 3, use_count: 4, tags: [] },
      { pool: "ssoHeavy", status: "active", quota: 999, consumed: 50, use_count: 7, tags: ["nsfw"] },
      { pool: "ssoSuper", status: "cooling", quota: 140, consumed: 8, use_count: 2, tags: [] },
    ],
    false
  );

  assert.equal(stats.localChatQuota, 80);
  assert.equal(stats.localImageQuota, 40);
  assert.equal(stats.totalCalls, 13);
  assert.equal(stats.nsfwTokens, 1);
});

test("getQuotaDisplayState shows upstream-managed badge for ssoHeavy", () => {
  const state = tokenUi.getQuotaDisplayState({ pool: "ssoHeavy", quota: 140 }, false);

  assert.equal(state.text, "上游");
  assert.equal(state.muted, true);
  assert.match(state.title, /上游/);
});

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

global.window = {
  addEventListener() {},
};

const accountUi = require(path.join(
  __dirname,
  "..",
  "_public",
  "static",
  "admin",
  "js",
  "account.js"
));

test("getLocalQuotaDisplay marks ssoHeavy as upstream-managed", () => {
  const state = accountUi.getLocalQuotaDisplay({ pool: "ssoHeavy", quota: 140 });

  assert.equal(accountUi.isUpstreamQuotaPool("ssoHeavy"), true);
  assert.equal(state.text, "上游");
  assert.equal(state.muted, true);
  assert.match(state.title, /ssoHeavy/);
});

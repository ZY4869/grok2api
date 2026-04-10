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

function createAccounts(count) {
  return Array.from({ length: count }, (_, index) => ({
    token: `token-${index + 1}`,
  }));
}

test("paginateAccounts returns 10 accounts on the first page by default size", () => {
  const page = accountUi.paginateAccounts(createAccounts(35), 1, 10);

  assert.equal(page.page, 1);
  assert.equal(page.pageSize, 10);
  assert.equal(page.totalPages, 4);
  assert.equal(page.items.length, 10);
  assert.equal(page.items[0].token, "token-1");
  assert.equal(page.items[9].token, "token-10");
});

test("paginateAccounts supports 20 and 30 item page sizes", () => {
  const page20 = accountUi.paginateAccounts(createAccounts(35), 2, 20);
  const page30 = accountUi.paginateAccounts(createAccounts(35), 2, 30);

  assert.equal(page20.totalPages, 2);
  assert.equal(page20.items.length, 15);
  assert.equal(page20.items[0].token, "token-21");

  assert.equal(page30.totalPages, 2);
  assert.equal(page30.items.length, 5);
  assert.equal(page30.items[0].token, "token-31");
});

test("paginateAccounts clamps invalid page and unsupported page size", () => {
  const page = accountUi.paginateAccounts(createAccounts(25), 99, 15);

  assert.equal(page.pageSize, 10);
  assert.equal(page.totalPages, 3);
  assert.equal(page.page, 3);
  assert.equal(page.items.length, 5);
  assert.equal(page.items[0].token, "token-21");
});

test("account page size persistence stores valid values and falls back on invalid ones", () => {
  const storage = new Map();
  global.localStorage = {
    getItem(key) {
      return storage.has(key) ? storage.get(key) : null;
    },
    setItem(key, value) {
      storage.set(key, String(value));
    },
  };

  assert.equal(accountUi.loadAccountPageSize(), 10);

  accountUi.saveAccountPageSize(20);
  assert.equal(storage.get("accountPageSize"), "20");
  assert.equal(accountUi.loadAccountPageSize(), 20);

  storage.set("accountPageSize", "999");
  assert.equal(accountUi.loadAccountPageSize(), 10);

  accountUi.saveAccountPageSize(15);
  assert.equal(storage.get("accountPageSize"), "10");

  delete global.localStorage;
});

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const accountImport = require(path.join(
  __dirname,
  "..",
  "_public",
  "static",
  "admin",
  "js",
  "account-import.js"
));

test("parseCsvText supports legacy two-column CSV", () => {
  const parsed = accountImport.parseCsvText([
    "token,pool",
    "token-1,ssoBasic",
    "token-2,ssoHeavy",
  ].join("\n"));

  assert.equal(parsed.entries.length, 2);
  assert.deepEqual(parsed.entries[0], {
    token: "token-1",
    pool: "ssoBasic",
    nsfwRequested: false,
    email: "",
  });
  assert.deepEqual(parsed.entries[1], {
    token: "token-2",
    pool: "ssoHeavy",
    nsfwRequested: false,
    email: "",
  });
});

test("parseCsvText supports nsfw and email columns", () => {
  const parsed = accountImport.parseCsvText([
    "token,pool,nsfw,email",
    "token-1,ssoBasic,YES,user1@example.com",
    "token-2,,no,user2@example.com",
  ].join("\n"));

  assert.equal(parsed.entries.length, 2);
  assert.equal(parsed.entries[0].nsfwRequested, true);
  assert.equal(parsed.entries[0].email, "user1@example.com");
  assert.equal(parsed.entries[1].pool, "");
  assert.equal(parsed.entries[1].nsfwRequested, false);
});

test("resolveEntryPools falls back to the selected pool", () => {
  const entries = accountImport.resolveEntryPools(
    [{ token: "token-1", pool: "", nsfwRequested: false, email: "" }],
    "ssoHeavy"
  );

  assert.equal(entries[0].pool, "ssoHeavy");
});

test("mergeImportEntries lets CSV rows override manual entries", () => {
  const merged = accountImport.mergeImportEntries(
    [{ token: "token-1", pool: "ssoBasic", nsfwRequested: false, email: "" }],
    [{ token: "token-1", pool: "ssoSuper", nsfwRequested: true, email: "user@example.com" }]
  );

  assert.equal(merged.length, 1);
  assert.deepEqual(merged[0], {
    token: "token-1",
    pool: "ssoSuper",
    nsfwRequested: true,
    email: "user@example.com",
  });
});

test("parseCsvText skips invalid blank-token rows", () => {
  const parsed = accountImport.parseCsvText([
    "token,pool,nsfw,email",
    ",ssoBasic,yes,user@example.com",
    "token-1,ssoBasic,no,user@example.com",
  ].join("\n"));

  assert.equal(parsed.entries.length, 1);
  assert.equal(parsed.skippedLines, 1);
  assert.equal(parsed.entries[0].token, "token-1");
});

test("isCsvFile accepts csv extension and common csv mime types", () => {
  assert.equal(accountImport.isCsvFile({ name: "tokens.csv", type: "" }), true);
  assert.equal(accountImport.isCsvFile({ name: "tokens.txt", type: "text/csv" }), true);
  assert.equal(accountImport.isCsvFile({ name: "tokens.txt", type: "application/vnd.ms-excel" }), true);
  assert.equal(accountImport.isCsvFile({ name: "tokens.txt", type: "text/plain" }), false);
});

test("pickCsvFile returns the first csv-like file", () => {
  const file = accountImport.pickCsvFile([
    { name: "notes.txt", type: "text/plain" },
    { name: "tokens.csv", type: "" },
    { name: "another.csv", type: "text/csv" },
  ]);

  assert.deepEqual(file, { name: "tokens.csv", type: "" });
});

test("prepareImportPayload keeps existing fields and only schedules missing nsfw tags", () => {
  const prepared = accountImport.prepareImportPayload(
    {
      ssoBasic: [
        {
          token: "token-1",
          status: "active",
          tags: ["nsfw"],
          note: "keep-me",
        },
      ],
    },
    [
      { token: "token-1", pool: "ssoSuper", nsfwRequested: true, email: "" },
      { token: "token-2", pool: "ssoBasic", nsfwRequested: true, email: "" },
    ]
  );

  assert.equal(prepared.addedCount, 1);
  assert.equal(prepared.existingCount, 1);
  assert.deepEqual(prepared.nsfwTargets, ["token-2"]);
  assert.equal(prepared.payload.ssoBasic.length, 1);
  assert.equal(prepared.payload.ssoSuper.length, 1);
  assert.equal(prepared.payload.ssoSuper[0].note, "keep-me");
  assert.equal(prepared.payload.ssoSuper[0].email, "");
});

test("prepareImportPayload persists csv email onto token payload", () => {
  const prepared = accountImport.prepareImportPayload(
    {
      ssoBasic: [{ token: "token-1", status: "active" }],
    },
    [{ token: "token-1", pool: "ssoBasic", nsfwRequested: false, email: "user@example.com" }]
  );

  assert.equal(prepared.payload.ssoBasic[0].email, "user@example.com");
});

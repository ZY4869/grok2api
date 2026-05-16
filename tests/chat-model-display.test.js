const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function createElementStub() {
  return {
    textContent: "",
    innerHTML: "",
    className: "",
    dataset: {},
    style: {},
    value: "",
    disabled: false,
    files: null,
    title: "",
    appendChild() {},
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    focus() {},
    select() {},
    closest() { return null; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    classList: {
      add() {},
      remove() {},
      toggle() {},
      contains() { return false; },
    },
  };
}

function loadChatHelpers() {
  const filePath = path.join(
    __dirname,
    "..",
    "_public",
    "static",
    "function",
    "js",
    "chat.js"
  );
  const code = fs.readFileSync(filePath, "utf8");
  const elements = new Map();
  const getElement = (id) => {
    if (!elements.has(id)) {
      elements.set(id, createElementStub());
    }
    return elements.get(id);
  };

  const sandbox = {
    module: { exports: {} },
    exports: {},
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    crypto: { randomUUID: () => "uuid" },
    fetch: async () => ({ ok: false }),
    showToast() {},
    t(key) { return key; },
    ensureFunctionKey: async () => "test-key",
    buildAuthHeaders: () => ({}),
    localStorage: {
      getItem() { return null; },
      setItem() {},
      removeItem() {},
    },
    window: {
      location: { href: "" },
      open() {},
      matchMedia() { return { matches: false }; },
    },
    document: {
      getElementById: getElement,
      createElement: () => createElementStub(),
      addEventListener() {},
      querySelectorAll() { return []; },
      body: { appendChild() {}, removeChild() {} },
      scrollingElement: { scrollTop: 0, scrollHeight: 0 },
      documentElement: { scrollTop: 0, scrollHeight: 0 },
    },
    navigator: {
      clipboard: {
        writeText: async () => {},
      },
    },
    TextDecoder,
    Blob,
    URL: {
      createObjectURL() { return "blob:test"; },
      revokeObjectURL() {},
    },
  };

  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: filePath });
  return sandbox.module.exports;
}

test("getModelDisplayName returns friendly names while preserving ids for fallback", () => {
  const chatUi = loadChatHelpers();

  assert.equal(chatUi.getModelDisplayName("grok-auto"), "Grok Auto");
  assert.equal(chatUi.getModelDisplayName("grok-3-fast"), "Grok Fast");
  assert.equal(chatUi.getModelDisplayName("grok-4-expert"), "Grok Expert");
  assert.equal(chatUi.getModelDisplayName("grok-4-heavy"), "Grok Heavy");
  assert.equal(chatUi.getModelDisplayName("grok-imagine-1.0-fast"), "Grok Image Fast");
  assert.equal(chatUi.getModelDisplayName("unknown-model"), "unknown-model");
});

"""Frontend composer tests: request lifecycle and input preservation.

Runs the real ``app.js`` in Node under a minimal DOM/fetch harness so the
behavior is proven rather than asserted from source text. Skipped when Node is
not installed; the rest of the suite stays Python-only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP_JS = REPO / "src" / "sws_agent" / "simulator" / "static" / "app.js"

_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const mode = process.argv[3] || "fail";

function makeNode(tag) {
  return {
    tag: tag || "div",
    className: "",
    textContent: "",
    title: "",
    disabled: false,
    value: "",
    placeholder: "",
    scrollTop: 0,
    scrollHeight: 0,
    children: [],
    parent: null,
    _handlers: {},
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute() {},
    appendChild(child) {
      child.parent = this;
      this.children.push(child);
      return child;
    },
    remove() {
      if (!this.parent) return;
      const i = this.parent.children.indexOf(this);
      if (i !== -1) this.parent.children.splice(i, 1);
      this.parent = null;
    },
    querySelector(selector) {
      const cls = selector.replace(/^\./, "");
      function walk(node) {
        for (let i = 0; i < node.children.length; i += 1) {
          const c = node.children[i];
          if ((" " + c.className + " ").indexOf(" " + cls + " ") !== -1) return c;
          const deeper = walk(c);
          if (deeper) return deeper;
        }
        return null;
      }
      return walk(this);
    },
    addEventListener(type, fn) {
      (this._handlers[type] = this._handlers[type] || []).push(fn);
    },
    requestSubmit() {
      if (this.disabled) return; // a disabled submit button does nothing
      (this._handlers.submit || []).forEach(function (fn) {
        fn({ preventDefault: function () {} });
      });
    },
    focus() {},
  };
}

const elements = {};
["chat-log", "trace", "tools", "chat-form", "message-input", "commands",
 "mcp-endpoint", "mode-badge", "demo-footer", "voice-toggle", "mute-toggle",
 "voice-status", "send-button"].forEach(function (id) { elements[id] = makeNode(); });
elements["send-button"].tag = "button";

const documentStub = {
  getElementById(id) { return elements[id] || null; },
  createElement(tag) { return makeNode(tag); },
};

let chatCalls = 0;
let resolveChat = null;

function fetchStub(url) {
  if (url === "/api/chat") {
    chatCalls += 1;
    if (mode === "dup") {
      // A request that stays in flight until the test resolves it, so the
      // harness can poke at the composer while the first call is pending.
      return new Promise(function (resolve) {
        resolveChat = resolve;
      }).then(function (body) {
        return { json: function () { return Promise.resolve(body); } };
      });
    }
    if (mode === "fail") return Promise.reject(new Error("connection refused"));
    return Promise.resolve({
      json: function () { return Promise.resolve({ reply: "ok", blocks: [], trace: [] }); },
    });
  }
  const body = {
    "/api/meta": {},
    "/api/tools": { tools: [] },
    "/api/commands": {
      commands: mode === "dup" ? ["audit my workspace"] : [],
    },
  }[url] || {};
  return Promise.resolve({ json: function () { return Promise.resolve(body); } });
}

const sandbox = {
  document: documentStub,
  fetch: fetchStub,
  SpeechSynthesisUtterance: function (t) { this.text = t; },
  console: console,
  setTimeout: setTimeout,
};
sandbox.window = sandbox;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "app.js" });

const input = elements["message-input"];
const chatLog = elements["chat-log"];
const sendButton = elements["send-button"];

function submitWith(text) {
  input.value = text;
  const handlers = elements["chat-form"]._handlers.submit || [];
  handlers.forEach(function (fn) {
    fn({ preventDefault: function () {} });
  });
}
function clickChip(index) {
  const chips = elements["commands"].children;
  const chip = chips[index || 0];
  if (!chip) { console.error("FAIL: no chip rendered at index " + (index || 0)); process.exit(1); }
  (chip._handlers.click || []).forEach(function (fn) { fn(); });
}
function assert(cond, msg) {
  if (!cond) { console.error("FAIL: " + msg); process.exit(1); }
}
function tick() {
  return new Promise(function (r) { setTimeout(r, 0); });
}
async function settle() {
  await tick(); await tick(); await tick();
}
function findEmptyState() {
  return chatLog.querySelector(".chat-empty-state");
}
// The visible message text now lives in a child .msg-text span, so read that
// rather than the wrapper (which also carries the .msg-meta role label).
function msgTexts() {
  return chatLog.children.map(function (n) {
    const t = n.querySelector(".msg-text");
    return String(t ? t.textContent : n.textContent);
  });
}
function childTexts() {
  // The harness does not aggregate child textContent the way a real DOM does,
  // so collect text across the node's subtree.
  function collect(node) {
    let out = String(node.textContent || "");
    node.children.forEach(function (c) { out += collect(c); });
    return out;
  }
  return chatLog.children.map(collect);
}
// Empty state exists at start and is never counted as a message.
function assertEmptyStatePresent() {
  assert(findEmptyState() !== null, "empty-state placeholder is present before any message");
  assert(chatLog.children.length === 1, "empty state is the only initial chat-log child");
}
function assertEmptyStateGone() {
  assert(findEmptyState() === null, "empty-state placeholder is gone once a message rendered");
}

(async function main() {
  const message = "audit my workspace";

  // Initial state: the empty-state placeholder is present and is NOT a message.
  assertEmptyStatePresent();

  if (mode === "dup") {
    await settle(); // let loadCommands render the chips

    // First submission: two rapid submits in the same tick must still be one
    // request (the composer is cleared before send() is even called).
    submitWith(message);
    submitWith(message);
    await settle();
    assert(chatCalls === 1, "rapid double submit launched " + chatCalls + " requests, expected 1");
    assert(sendButton.disabled === true, "Send is disabled while a request is in flight");
    assertEmptyStateGone();

    // A suggested-prompt chip clicked mid-request must not start a second
    // concurrent request, and must not swallow the user's typed text either.
    input.value = "half-typed follow up";
    clickChip(0);
    await settle();
    assert(chatCalls === 1,
      "chip clicked mid-request launched a duplicate request (" + chatCalls + ")");
    assert(input.value === "half-typed follow up",
      "chip clicked mid-request must not overwrite what the user is typing");

    // Finish the in-flight request: Send must come back and the composer must
    // be usable again, with exactly one exchange rendered.
    resolveChat({ reply: "ok", blocks: [], trace: [] });
    await settle();
    assert(chatCalls === 1, "exactly one request total after the pending one resolves");
    assert(sendButton.disabled === false, "Send is restored after the request succeeds");
    assert(input.disabled === false, "composer input is restored after the request succeeds");
    const texts = msgTexts();
    assert(texts.filter(function (t) { return t === message; }).length === 1,
      "the user message is rendered exactly once");
    assert(texts.indexOf("ok") !== -1, "the assistant reply is rendered");
    assertEmptyStateGone();

    // A failed request must also restore Send.
    submitWith(message);
    await settle();
    assert(sendButton.disabled === true, "Send is disabled again for the next request");
    // Still absent after subsequent messages: a renderer refactor must not
    // resurrect the placeholder.
    assertEmptyStateGone();
    console.log("OK");
    process.exit(0);
  }

  submitWith(message);
  await settle();
  assert(chatCalls === 1, "one request was sent for mode " + mode);
  assertEmptyStateGone();

  if (mode === "fail") {
    assert(input.value === message,
      "user's typed message is preserved in the composer after a failed request");
    assert(sendButton.disabled === false, "Send is restored after a failed request");
    const texts = childTexts();
    assert(texts.some(function (t) { return t.indexOf("could not reach") !== -1; }),
      "an understandable error was shown");
    assert(input.disabled === false, "composer is usable again after failure");
  } else {
    assert(input.value === "", "successful send still clears the composer");
    assert(sendButton.disabled === false, "Send is restored after a successful request");
    const texts = msgTexts();
    assert(texts.indexOf(message) !== -1, "the user message is rendered");
    assert(texts.indexOf("ok") !== -1, "the assistant reply is rendered");

    // Role distinction: user and assistant messages keep their exact classes
    // and carry a .msg-meta role label; the visible text stays in .msg-text.
    const first = chatLog.children[0];
    const second = chatLog.children[1];
    assert((" " + first.className + " ").indexOf(" user ") !== -1,
      "user message keeps the exact 'msg user' classes");
    assert((" " + second.className + " ").indexOf(" assistant ") !== -1,
      "assistant message keeps the exact 'msg assistant' classes");
    assert(first.querySelector(".msg-meta") !== null, "user message has a role label");
    assert(second.querySelector(".msg-meta") !== null, "assistant message has a role label");
  }

  console.log("OK");
  process.exit(0);
})();
"""


def _run(mode: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / f"composer_{mode}.cjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["node", str(harness), str(APP_JS), mode],
        capture_output=True,
        text=True,
    )


_requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is not installed"
)


@_requires_node
def test_failed_request_keeps_the_users_message_in_the_composer(
    tmp_path: Path,
) -> None:
    result = _run("fail", tmp_path)
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("OK")


@_requires_node
def test_successful_send_still_clears_the_composer(tmp_path: Path) -> None:
    result = _run("ok", tmp_path)
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("OK")


@_requires_node
def test_rapid_resubmission_never_launches_duplicate_requests(tmp_path: Path) -> None:
    result = _run("dup", tmp_path)
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("OK")

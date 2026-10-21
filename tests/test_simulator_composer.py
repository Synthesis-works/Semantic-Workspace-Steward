"""Frontend composer tests: a failed request must not destroy the user's input.

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
    _handlers: {},
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute() {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(type, fn) {
      (this._handlers[type] = this._handlers[type] || []).push(fn);
    },
    requestSubmit() {},
    focus() {},
  };
}

const elements = {};
["chat-log", "trace", "tools", "chat-form", "message-input", "commands",
 "mcp-endpoint", "mode-badge", "demo-footer", "voice-toggle", "mute-toggle",
 "voice-status"].forEach(function (id) { elements[id] = makeNode(); });

const documentStub = {
  getElementById(id) { return elements[id] || null; },
  createElement(tag) { return makeNode(tag); },
};

let chatCalls = 0;

function fetchStub(url) {
  if (url === "/api/chat") {
    chatCalls += 1;
    if (mode === "fail") return Promise.reject(new Error("connection refused"));
    return Promise.resolve({
      json: function () { return Promise.resolve({ reply: "ok", blocks: [], trace: [] }); },
    });
  }
  const body = {
    "/api/meta": {},
    "/api/tools": { tools: [] },
    "/api/commands": { commands: [] },
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

function submitWith(text) {
  input.value = text;
  const handlers = elements["chat-form"]._handlers.submit || [];
  handlers.forEach(function (fn) {
    fn({ preventDefault: function () {} });
  });
}
function assert(cond, msg) {
  if (!cond) { console.error("FAIL: " + msg); process.exit(1); }
}
function tick() {
  return new Promise(function (r) { setTimeout(r, 0); });
}

(async function main() {
  const message = "audit my workspace";
  submitWith(message);
  await tick();
  await tick();
  await tick();
  assert(chatCalls === 1, "one request was sent for mode " + mode);

  if (mode === "fail") {
    assert(input.value === message,
      "user's typed message is preserved in the composer after a failed request");
    const texts = chatLog.children.map(function (n) { return String(n.textContent); });
    assert(texts.some(function (t) { return t.indexOf("could not reach") !== -1; }),
      "an understandable error was shown");
    assert(input.disabled === false, "composer is usable again after failure");
  } else {
    assert(input.value === "", "successful send still clears the composer");
    const texts = chatLog.children.map(function (n) { return String(n.textContent); });
    assert(texts.indexOf(message) !== -1, "the user message is rendered");
    assert(texts.indexOf("ok") !== -1, "the assistant reply is rendered");
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

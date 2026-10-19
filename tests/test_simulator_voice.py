"""Hermetic behavioral test for the demo's browser voice lifecycle.

The bug this guards against: while ``speechSynthesis`` speaks an assistant
reply, the still-active ``SpeechRecognition`` engine hears the assistant's own
audio and transcribes it back into the chat composer.

There is no JavaScript runtime in the Python toolchain, so this test executes
the real ``app.js`` in Node under a minimal DOM/speech harness and asserts the
lifecycle directly. It is skipped when Node is not installed; the rest of the
suite stays fully hermetic and Python-only.
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

const order = [];
let chatCalls = 0;

function makeNode(tag) {
  return {
    tag: tag || "div",
    className: "",
    textContent: "",
    title: "",
    disabled: false,
    value: "",
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

const recognitions = [];
class FakeRecognition {
  constructor() {
    this.lang = "";
    this.continuous = false;
    this.interimResults = false;
    this.onresult = null;
    this.onerror = null;
    this.onend = null;
    recognitions.push(this);
  }
  start() { order.push("start"); }
  stop() {
    order.push("stop");
    if (this.onend) this.onend();
  }
}

const synth = {
  spoken: [],
  cancel() {},
  getVoices() { return [{ lang: "en-US" }]; },
  speak(u) { order.push("speak"); this.spoken.push(u); },
};

class FakeUtterance {
  constructor(text) { this.text = text; this.onend = null; this.onerror = null; }
}

function fetchStub(url) {
  const reply = "Cloud Action Briefing for web-assets-api: run an audit.";
  const body = {
    "/api/chat": { reply: reply, blocks: [], trace: [] },
    "/api/meta": {},
    "/api/tools": { tools: [] },
    "/api/commands": { commands: [] },
  }[url] || {};
  if (url === "/api/chat") chatCalls += 1;
  return Promise.resolve({
    json: function () { return Promise.resolve(body); },
  });
}

const sandbox = {
  document: documentStub,
  fetch: fetchStub,
  SpeechSynthesisUtterance: FakeUtterance,
  console: console,
  setTimeout: setTimeout,
};
sandbox.window = sandbox;
sandbox.window.SpeechRecognition = FakeRecognition;
sandbox.window.speechSynthesis = synth;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "app.js" });

const input = elements["message-input"];
const voiceToggle = elements["voice-toggle"];

function click(node) {
  (node._handlers.click || []).forEach(function (fn) { fn(); });
}
function finalResult(instance, text) {
  const result = [{ transcript: text }];
  result.isFinal = true;
  instance.onresult({ resultIndex: 0, results: [result] });
}
function count(marker) {
  return order.filter(function (m) { return m === marker; }).length;
}
function assert(cond, msg) {
  if (!cond) { console.error("FAIL: " + msg); process.exit(1); }
}
function tick() {
  return new Promise(function (resolve) { setTimeout(resolve, 0); });
}

(async function main() {
  // 1. Voice input on -> recognition starts listening.
  click(voiceToggle);
  assert(recognitions.length === 1, "one recognition instance");
  const rec = recognitions[0];
  assert(count("start") === 1, "recognition started when voice enabled");

  // 2. The user speaks; the reply is fetched and then spoken.
  const r = [{ transcript: "audit my workspace" }];
  r.isFinal = true;
  rec.onresult({ resultIndex: 0, results: [r] });
  await tick();
  await tick();
  assert(chatCalls === 1, "one chat request for the user's speech");
  assert(synth.spoken.length === 1, "assistant reply was spoken");
  assert(order.lastIndexOf("stop") < order.indexOf("speak"),
    "recognition stopped BEFORE TTS spoke");

  // 3. Regression: while TTS is still speaking, the assistant's own audio must
  //    be ignored even if the browser fires a result or ends recognition.
  const startsBefore = count("start");
  input.value = "";
  finalResult(rec, "cloud action briefing run an audit");
  rec.onend();
  await tick();
  assert(input.value === "", "assistant audio is NOT transcribed into the composer");
  assert(chatCalls === 1, "no chat request from the assistant's own audio");
  assert(count("start") === startsBefore,
    "onend during TTS does not restart recognition");

  // 4. TTS ends -> recognition resumes exactly once.
  const utterance = synth.spoken[0];
  assert(typeof utterance.onend === "function", "utterance has an onend handler");
  utterance.onend();
  assert(count("start") === startsBefore + 1,
    "recognition resumed once after TTS ended");

  // 5. Listening and speaking resume normally afterwards.
  finalResult(rec, "show my pending approvals");
  await tick();
  await tick();
  assert(chatCalls === 2, "listening resumes normally after TTS");
  assert(synth.spoken.length === 2, "second reply was spoken");
  assert(order.lastIndexOf("stop") < order.lastIndexOf("speak"),
    "the second reply also stopped the mic before speaking");

  console.log("OK");
  process.exit(0);
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_voice_tts_cannot_feed_back_into_the_composer(tmp_path: Path) -> None:
    harness = tmp_path / "voice_harness.cjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["node", str(harness), str(APP_JS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("OK")

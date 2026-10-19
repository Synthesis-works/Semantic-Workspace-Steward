(function () {
  "use strict";

  const logEl = document.getElementById("chat-log");
  const traceEl = document.getElementById("trace");
  const toolsEl = document.getElementById("tools");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("message-input");
  const commandsEl = document.getElementById("commands");
  const endpointEl = document.getElementById("mcp-endpoint");
  const modeEl = document.getElementById("mode-badge");
  const footerEl = document.getElementById("demo-footer");
  const voiceToggleEl = document.getElementById("voice-toggle");
  const muteToggleEl = document.getElementById("mute-toggle");
  const voiceStatusEl = document.getElementById("voice-status");

  const VOICE_NOTE =
    "Voice Mode \u2014 Alexa+ inspired simulation (browser speech; not an " +
    "Alexa+ integration).";

  let history = [];
  let recognition = null;
  let voiceOn = false;
  let muted = false;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function appendMessage(kind, text) {
    logEl.appendChild(el("div", "msg " + kind, text));
    logEl.scrollTop = logEl.scrollHeight;
  }

  function appendBlock(block) {
    const wrapper = el("div", "block " + (block.type || ""));
    const title = el("h3");
    switch (block.type) {
      case "summary":
        title.textContent = "Workspace audit";
        wrapper.appendChild(title);
        wrapper.appendChild(
          el("div", "kv", "snapshot: " + (block.snapshot_id || "-"))
        );
        wrapper.appendChild(el("div", "kv", "resources collected: " + block.resource_count));
        block.attention.forEach(function (item) {
          wrapper.appendChild(
            el("div", "kv",
              "→ " + item.resource_id + "  [" + item.action + "]  " + (item.rule || ""))
          );
        });
        break;
      case "attention":
        title.textContent = "Resources needing attention";
        wrapper.appendChild(title);
        if (!block.resources.length) {
          wrapper.appendChild(el("div", "kv", "none"));
          break;
        }
        const list = el("ul");
        block.resources.forEach(function (row) {
          const item = el(
            "li",
            "kv",
            row.name +
              " → " +
              row.action +
              (row.rule ? "  (" + row.rule + ")" : "") +
              "  —  " +
              row.rationale
          );
          list.appendChild(item);
        });
        wrapper.appendChild(list);
        break;
      case "tickets":
        title.textContent = "Pending approval tickets";
        wrapper.appendChild(title);
        if (!block.tickets.length) {
          wrapper.appendChild(el("div", "kv", "none"));
          break;
        }
        const tlist = el("ul");
        block.tickets.forEach(function (ticket) {
          const item = el(
            "li",
            "kv",
            ticket.ticket_id +
              " — " +
              ticket.resource_id +
              " (" +
              ticket.action +
              "): " +
              (ticket.rationale || "")
          );
          tlist.appendChild(item);
        });
        wrapper.appendChild(tlist);
        break;
      case "ticket_update":
        title.textContent = "Ticket updated";
        wrapper.appendChild(title);
        const t = block.ticket || {};
        wrapper.appendChild(
          el("div", "kv", t.ticket_id + " → status " + t.status + " (decided: " + block.decision + ")")
        );
        wrapper.appendChild(
          el("div", "kv", "No AWS action executed; in-memory demo store updated only.")
        );
        break;
      case "explanation":
        title.textContent = "Interpretive demo explanation (not policy truth)";
        wrapper.appendChild(title);
        const d = block.decision || {};
        const e = block.explanation || {};
        wrapper.appendChild(
          el("div", "kv", block.name + " → " + d.recommended_action + (d.rule ? " (rule: " + d.rule + ")" : ""))
        );
        wrapper.appendChild(el("div", "kv", "explanation [" + e.provider + "]: " + (e.text || "")));
        break;
      case "briefing":
        title.textContent = "Cloud Action Briefing";
        wrapper.appendChild(title);
        wrapper.appendChild(
          el("div", "kv", "Resource: " + (block.name || block.resource_id || "-")
            + (block.resource_type ? " (" + block.resource_type + ")" : ""))
        );
        wrapper.appendChild(el("div", "kv", "Observed state: " + (block.state || "unknown")));
        if (block.policy) {
          wrapper.appendChild(
            el("div", "kv", "Policy finding: " + (block.policy.action || "-")
              + (block.policy.rule ? " (rule " + block.policy.rule + ")" : ""))
          );
          if (block.policy.rationale) {
            wrapper.appendChild(el("div", "kv", "Why it matters: " + block.policy.rationale));
          }
        } else if (block.explanation && block.explanation.text) {
          wrapper.appendChild(el("div", "kv", "Why it matters: " + block.explanation.text));
        }
        wrapper.appendChild(
          el("div", "kv", "Proposed action: " + (block.proposed_action || "none"))
        );
        const auth = block.authorization || {};
        wrapper.appendChild(
          el("div", "kv", "Authorization: " + (auth.decision || "not evaluated")
            + (auth.requires_human_approval ? " — human approval required" : ""))
        );
        if (block.ticket && block.ticket.ticket_id) {
          wrapper.appendChild(
            el("div", "kv", "Approval ticket: " + block.ticket.ticket_id
              + " (" + (block.ticket.status || "pending") + ")")
          );
        }
        wrapper.appendChild(
          el("div", "kv", "Execution: not available from the public demo")
        );
        break;
      case "error":
        title.textContent = "Error";
        wrapper.appendChild(el("div", "kv", block.message || "unknown error"));
        break;
      default:
        wrapper.appendChild(el("div", "kv", JSON.stringify(block)));
    }
    logEl.appendChild(wrapper);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function renderTrace(trace) {
    trace.forEach(function (event) {
      const label = event.state === "success" ? "✓ " : event.state === "error" ? "✗ " : "";
      const item = el("li", event.state || "", label + event.tool + " — " + event.summary);
      traceEl.appendChild(item);
    });
    traceEl.scrollTop = traceEl.scrollHeight;
  }

  function renderTools(tools) {
    tools.forEach(function (tool) {
      toolsEl.appendChild(el("li", "", tool.name));
    });
  }

  async function loadMeta() {
    try {
      const meta = await fetch("/api/meta").then(function (r) {
        return r.json();
      });
      endpointEl.textContent = meta.mcp_endpoint || "";
      if (modeEl && meta.mode) {
        if (meta.mode === "llm") {
          modeEl.textContent =
            "conversational · " + (meta.provider_label || meta.provider || "llm");
          modeEl.title = meta.model
            ? "conversational provider: " + meta.model
            : "conversational provider";
          if (footerEl) {
            footerEl.textContent =
              "Synthetic SWS workspace: answers are produced by " +
              (meta.provider_label || meta.provider || "an LLM") +
              " over synthetic data. No real AWS resources, no real cost " +
              "figures, and no actions are executed.";
          }
        } else {
          modeEl.textContent = "deterministic";
          modeEl.title = "deterministic demo (no LLM configured)";
          if (footerEl) {
            footerEl.textContent =
              "Synthetic SWS workspace: answers are produced by a deterministic " +
              "simulation with no LLM. No real AWS resources, no real cost " +
              "figures, and no actions are executed.";
          }
        }
      }
    } catch (_) {
      /* meta is non-critical in a demo */
    }
    try {
      const data = await fetch("/api/tools").then(function (r) {
        return r.json();
      });
      renderTools(data.tools || []);
    } catch (_) {
      /* tools list is best-effort */
    }
  }

  async function loadCommands() {
    try {
      const data = await fetch("/api/commands").then(function (r) {
        return r.json();
      });
      (data.commands || []).forEach(function (command) {
        const chip = el("button", "chip", command);
        chip.addEventListener("click", function () {
          input.value = command;
          form.requestSubmit();
        });
        commandsEl.appendChild(chip);
      });
    } catch (_) {
      /* chips are convenience only */
    }
  }

  async function send(message) {
    appendMessage("user", message);
    input.disabled = true;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message, history: history }),
      }).then(function (r) {
        return r.json();
      });
      history.push({ role: "user", content: message });
      history.push({ role: "assistant", content: response.reply });
      appendMessage("assistant", response.reply);
      (response.blocks || []).forEach(appendBlock);
      renderTrace(response.trace || []);
      if (voiceOn && !muted) {
        speak(response.reply);
      }
    } catch (err) {
      appendMessage("assistant", "The demo could not reach the simulator: " + err);
    } finally {
      input.disabled = false;
      input.focus();
    }
  }

  // --- Voice Mode: Alexa+ inspired simulation (browser-native only) ---------
  // Input uses the browser's SpeechRecognition; output uses speechSynthesis.
  // Nothing here is Alexa: it is a simulated voice experience, feature-detected
  // with a graceful text-only fallback.

  function speechRecognitionCtor() {
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
  }

  function setVoiceStatus(text) {
    if (voiceStatusEl) voiceStatusEl.textContent = text;
  }

  function speak(text) {
    if (!window.speechSynthesis || !text) return;
    // Speak only the first paragraph so the demo disclaimer is not repeated.
    const spoken = String(text).split("\n\n")[0];
    if (!spoken) return;
    try {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(spoken);
      const voices = window.speechSynthesis.getVoices() || [];
      const english = voices.filter(function (v) {
        return /^en([-_]|$)/i.test(v.lang || "");
      });
      if (english.length) utterance.voice = english[0];
      utterance.rate = 1.0;
      window.speechSynthesis.speak(utterance);
    } catch (_) {
      /* speech output is best-effort */
    }
  }

  function stopSpeaking() {
    try {
      if (window.speechSynthesis) window.speechSynthesis.cancel();
    } catch (_) {
      /* ignore */
    }
  }

  function updateVoiceControls() {
    if (voiceToggleEl) {
      voiceToggleEl.classList.toggle("active", voiceOn);
      voiceToggleEl.setAttribute("aria-pressed", voiceOn ? "true" : "false");
      voiceToggleEl.textContent = voiceOn ? "Listening \u2014 tap to stop" : "Voice input";
    }
    if (muteToggleEl) {
      muteToggleEl.classList.toggle("active", muted);
      muteToggleEl.setAttribute("aria-pressed", muted ? "true" : "false");
      muteToggleEl.textContent = muted ? "Replies muted" : "Replies spoken";
    }
  }

  function setVoiceOn(on) {
    if (on && !recognition) {
      setVoiceStatus(
        "Voice input is not supported in this browser (try Chrome or Edge); " +
          "text input still works. " +
          VOICE_NOTE
      );
      return;
    }
    voiceOn = on;
    if (on) {
      try {
        recognition.start();
      } catch (_) {
        /* already starting */
      }
      setVoiceStatus("Listening\u2026 speak your request. " + VOICE_NOTE);
    } else {
      try {
        recognition.stop();
      } catch (_) {
        /* already stopped */
      }
      stopSpeaking();
      setVoiceStatus(VOICE_NOTE);
    }
    updateVoiceControls();
  }

  function setMuted(value) {
    muted = value;
    if (muted) stopSpeaking();
    updateVoiceControls();
  }

  function initVoice() {
    const Ctor = speechRecognitionCtor();
    if (!Ctor) {
      if (voiceToggleEl) {
        voiceToggleEl.disabled = true;
        voiceToggleEl.textContent = "Voice unavailable";
        voiceToggleEl.title = "Speech recognition is not supported here.";
      }
      setVoiceStatus(
        "Voice Mode \u2014 Alexa+ inspired simulation. Voice input is not " +
          "supported in this browser (try Chrome or Edge); text input works."
      );
      updateVoiceControls();
      return;
    }
    recognition = new Ctor();
    recognition.lang = "en-US";
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.onresult = function (event) {
      let interim = "";
      let finals = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const transcript = result[0] ? result[0].transcript : "";
        if (result.isFinal) finals += transcript;
        else interim += transcript;
      }
      if (interim) input.value = interim.trim();
      const spoken = finals.trim();
      if (spoken) {
        input.value = "";
        send(spoken);
      }
    };
    recognition.onerror = function (event) {
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        setVoiceOn(false);
        setVoiceStatus(
          "Microphone permission was denied \u2014 you can still type. " +
            VOICE_NOTE
        );
      }
    };
    recognition.onend = function () {
      // Browsers stop after a pause; keep listening while toggled on.
      if (voiceOn) {
        try {
          recognition.start();
        } catch (_) {
          /* restart is best-effort */
        }
      }
    };
    updateVoiceControls();
    setVoiceStatus(VOICE_NOTE);
  }

  if (voiceToggleEl) {
    voiceToggleEl.addEventListener("click", function () {
      setVoiceOn(!voiceOn);
    });
  }
  if (muteToggleEl) {
    muteToggleEl.addEventListener("click", function () {
      setMuted(!muted);
    });
  }
  initVoice();
  setMuted(false);

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    input.value = "";
    send(message);
  });

  loadCommands();
  loadMeta();
})();
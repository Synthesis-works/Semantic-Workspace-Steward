(function () {
  "use strict";

  const logEl = document.getElementById("chat-log");
  const traceEl = document.getElementById("trace");
  const toolsEl = document.getElementById("tools");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("message-input");
  const commandsEl = document.getElementById("commands");
  const endpointEl = document.getElementById("mcp-endpoint");

  let history = [];

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
      const item = el("li", event.state || "", event.tool + " — " + event.summary);
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
      endpointEl.textContent = meta.mcp_endpoint;
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
    } catch (err) {
      appendMessage("assistant", "The demo could not reach the simulator: " + err);
    } finally {
      input.disabled = false;
      input.focus();
    }
  }

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
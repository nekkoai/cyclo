// SPDX-License-Identifier: MIT

const DEFAULT_ROOT = "";
const LIVE_REFRESH_MS = 750;
const CHAT_REFRESH_MS = 1000;
const CHAT_STATE_REFRESH_MS = 3000;
const CHAT_TRANSCRIPT_MAX_BYTES = 512 * 1024;
const CHAT_MAX_MESSAGES = 80;
const CHAT_RENDER_TEXT_LIMIT = 180 * 1024;
const TRANSCRIPT_SECTION_TITLES = new Set(["User", "Assistant", "Thinking", "Reasoning", "Tool Call"]);

const state = {
  snapshot: null,
  selected: null,
  activeFile: null,
  live: false,
  liveTimer: null,
  chatTimer: null,
  chatDraft: "",
  chatAutofocused: false,
  chatWasBusy: false,
  chatPinned: true,
  chatMessageKeys: [],
  chatTranscriptInFlight: false,
  chatStateInFlight: false,
  chatStateLastRefresh: 0,
  view: initialView(),
  query: "",
  root: DEFAULT_ROOT
};

const columns = [
  ["intake", "Intake"],
  ["pending", "Pending"],
  ["active", "Active"],
  ["blocked", "Blocked"],
  ["closed", "Done"]
];

const els = {
  rootInput: document.querySelector("#rootInput"),
  searchInput: document.querySelector("#searchInput"),
  refreshButton: document.querySelector("#refreshButton"),
  railStatus: document.querySelector("#railStatus"),
  metrics: document.querySelector("#metrics"),
  chat: document.querySelector("#view-chat"),
  board: document.querySelector("#view-board"),
  pipeline: document.querySelector("#view-pipeline"),
  agents: document.querySelector("#view-agents"),
  map: document.querySelector("#view-map"),
  inspector: document.querySelector("#inspector"),
  toast: document.querySelector("#toast")
};

setRootDisplay(state.root);

document.querySelectorAll(".rail-button").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});

els.refreshButton.addEventListener("click", () => loadSnapshot());
els.searchInput.addEventListener("input", () => {
  state.query = els.searchInput.value.trim().toLowerCase();
  render();
});

window.addEventListener("keydown", (event) => {
  if (event.key === "Escape") clearSelection();
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    if (state.view === "chat") return;
    event.preventDefault();
    els.searchInput.focus();
  }
});
window.addEventListener("hashchange", () => setView(initialView(), { updateHash: false }));

setView(state.view, { updateHash: false });
loadSnapshot();
setInterval(() => loadSnapshot({ silent: true }), 30000);

async function loadSnapshot(options = {}) {
  const { silent = false } = options;
  try {
    setBusy(!silent);
    const response = await fetch("/api/snapshot");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "snapshot failed");

    state.snapshot = payload;
    state.root = payload.root;
    setRootDisplay(payload.root);
    els.railStatus.classList.remove("error");
    if (silent && state.view === "chat") {
      updateChatAgentState(consoleAgent());
    } else {
      render();
    }
    if (!silent) toast("Snapshot loaded");
  } catch (error) {
    els.railStatus.classList.add("error");
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

function setBusy(active) {
  els.refreshButton.disabled = active;
  els.refreshButton.style.opacity = active ? "0.55" : "";
}

function setRootDisplay(root) {
  els.rootInput.textContent = root || "";
  els.rootInput.title = root || "AgentWS root";
}

function setView(view, options = {}) {
  const { updateHash = true } = options;
  const previousView = state.view;
  state.view = view;
  if (view === "chat" && previousView !== "chat") {
    state.chatAutofocused = false;
    state.chatPinned = true;
  }
  if (updateHash && location.hash.slice(1) !== view) {
    history.replaceState(null, "", `#${view}`);
  }
  document.querySelectorAll(".rail-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  document.querySelectorAll(".view-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `view-${view}`);
  });
  updateToolbarForView();
  render();
}

function updateToolbarForView() {
  els.searchInput.closest(".search-field").hidden = state.view === "chat";
}

function initialView() {
  const view = location.hash.replace("#", "");
  return ["chat", "board", "pipeline", "agents", "map"].includes(view) ? view : "chat";
}

function render() {
  if (!state.snapshot) {
    renderEmptyChooser();
    return;
  }

  els.metrics.hidden = state.view === "chat";
  if (state.view !== "chat") renderMetrics();
  if (state.view !== "chat") stopChatTimer();
  if (state.view === "chat") renderChat();
  if (state.view === "board") renderBoard();
  if (state.view === "pipeline") renderPipeline();
  if (state.view === "agents") renderAgents();
  if (state.view === "map") renderMap();

  if (state.selected) {
    const stillExists = lookupSelection(state.selected.type, state.selected.id);
    if (stillExists) {
      state.selected.item = stillExists;
    } else {
      state.selected = null;
    }
  }
  renderInspector();
}

function renderEmptyChooser() {
  els.metrics.hidden = false;
  els.metrics.innerHTML = metricSkeleton();
  const message = `
    <div class="empty-note">
      Run <strong>tools/agentws</strong> and load the printed local URL.
    </div>
  `;
  els.chat.innerHTML = message;
  els.board.innerHTML = message;
  els.pipeline.innerHTML = message;
  els.agents.innerHTML = message;
  els.map.innerHTML = message;
}

function renderMetrics() {
  const { metrics, generatedAt } = state.snapshot;
  const activeJobs = (metrics.jobsByStatus.running || 0) + (metrics.jobsByStatus.claimed || 0);
  els.metrics.innerHTML = [
    metric(metrics.tasksTotal, "Tasks tracked", `${metrics.tasksByFlow.closed || 0} complete`),
    metric(activeJobs, "Active jobs", `${metrics.jobsByStatus.pending || 0} pending`),
    metric(metrics.activeAgents, "Agents working", `${metrics.agentsTotal} registered`),
    metric(metrics.failedJobs + metrics.attentionTasks, "Need attention", `Updated ${formatTime(generatedAt)}`)
  ].join("");
}

function metric(value, label, sublabel) {
  return `
    <article class="metric">
      <strong>${escapeHtml(value)}</strong>
      <span>${escapeHtml(label)}</span>
      <span>${escapeHtml(sublabel)}</span>
    </article>
  `;
}

function metricSkeleton() {
  return [0, 1, 2, 3].map(() => metric("...", "Loading", "")).join("");
}

function renderBoard() {
  const tasks = filteredTasks();
  const activity = filteredActivity();
  const byColumn = groupBy(tasks, (task) => task.flowState);

  els.board.innerHTML = `
    <div class="board-layout">
      <div class="board-statuses">
        ${columns.map(([key, label]) => renderColumn(key, label, byColumn.get(key) || [])).join("")}
      </div>
      <aside class="feed-panel">
        <div class="section-head">
          <h2>Live Feed</h2>
          <span class="count-pill">${activity.length}</span>
        </div>
        <div class="activity-list">
          ${activity.length ? activity.slice(0, 42).map(renderActivity).join("") : `<div class="empty-note">No matching activity</div>`}
        </div>
      </aside>
    </div>
  `;
  bindSelectionButtons(els.board);
}

function renderColumn(key, label, tasks) {
  return `
    <section class="status-section">
      <div class="column-head">
        <h2>${escapeHtml(label)}</h2>
        <span class="count-pill">${tasks.length}</span>
      </div>
      <div class="status-card-grid">
        ${tasks.length ? tasks.map(renderTaskCard).join("") : `<div class="empty-note">No tasks</div>`}
      </div>
    </section>
  `;
}

function renderTaskCard(task) {
  const tone = toneFor(task.flowState);
  const selected = isSelected("task", task.id) ? "selected" : "";
  return `
    <button class="task-card ${selected}" data-select-type="task" data-select-id="${escapeAttr(task.id)}">
      <div class="card-meta">
        <span class="status-pill status-${escapeAttr(task.flowState)} ${tone}">
          <span class="status-dot"></span>${escapeHtml(task.flowState)}
        </span>
        <span class="chip">${task.doneJobs}/${task.jobCount || 0} jobs</span>
      </div>
      <h3>${escapeHtml(task.title)}</h3>
      <p>${escapeHtml(task.objective || task.specPreview || "No task summary")}</p>
      <div class="progress" aria-label="${task.progress}% complete"><span style="width:${Math.max(4, task.progress)}%"></span></div>
      <div class="chip-row">
        ${task.roles.slice(0, 3).map((role) => `<span class="chip">${escapeHtml(role)}</span>`).join("")}
        ${task.failedJobs ? `<span class="chip tone-coral">${task.failedJobs} failed</span>` : ""}
      </div>
    </button>
  `;
}

function renderActivity(event) {
  const tone = toneFor(event.status);
  return `
    <div class="activity-item status-${escapeAttr(event.status)}">
      <span class="status-dot"></span>
      <button data-select-type="${escapeAttr(event.type)}" data-select-id="${escapeAttr(event.itemId)}">
        <h3>${escapeHtml(event.title)}</h3>
        <p>${escapeHtml(event.body || event.itemId)}</p>
        <div class="time">${escapeHtml(formatDateTime(event.timestamp))}</div>
        <div class="chip-row"><span class="chip ${tone}">${escapeHtml(event.type)}</span><span class="chip">${escapeHtml(event.taskId || event.itemId)}</span></div>
      </button>
    </div>
  `;
}

function renderPipeline() {
  const tasks = filteredTasks().filter((task) => task.jobCount > 0);
  const jobsByTask = groupBy(filteredJobs(), (job) => job.taskId);

  els.pipeline.innerHTML = `
    <div class="pipeline-list">
      ${tasks.length ? tasks.map((task) => renderPipelineRow(task, jobsByTask.get(task.id) || [])).join("") : `<div class="empty-note">No matching job pipelines</div>`}
    </div>
  `;
  bindSelectionButtons(els.pipeline);
}

function renderPipelineRow(task, jobs) {
  const jobsByStage = groupBy(jobs, (job) => job.stage);
  return `
    <article class="pipeline-row">
      <button class="pipeline-title" data-select-type="task" data-select-id="${escapeAttr(task.id)}">
        <h2>${escapeHtml(task.title)}</h2>
        <p>${escapeHtml(task.id)}</p>
        <div class="chip-row">
          <span class="chip">${task.jobCount} jobs</span>
          <span class="chip ${toneFor(task.flowState)}">${task.flowState}</span>
        </div>
      </button>
      <div class="pipeline-track">
        ${stagesForJobs(jobs).map(([stage, label]) => renderStageLane(stage, label, jobsByStage.get(stage) || [])).join("")}
      </div>
    </article>
  `;
}

function stagesForJobs(jobs) {
  const ordered = [...jobs].sort((a, b) => {
    const aTime = Date.parse(a.firstEventAt || a.updatedAt || 0) || 0;
    const bTime = Date.parse(b.firstEventAt || b.updatedAt || 0) || 0;
    return aTime - bTime || a.id.localeCompare(b.id);
  });
  const stages = [...new Set(ordered.map((job) => job.stage).filter(Boolean))];
  return stages.map((stage) => [stage, titleCase(stage)]);
}

function renderStageLane(stage, label, jobs) {
  return `
    <div class="stage-lane">
      <div class="stage-label">${escapeHtml(label)}</div>
      ${jobs.length ? jobs.map(renderPipelineJob).join("") : `<div class="pipeline-job" aria-hidden="true"><small>empty</small></div>`}
    </div>
  `;
}

function renderPipelineJob(job) {
  const selected = isSelected("job", job.id) ? "selected" : "";
  return `
    <button class="pipeline-job ${selected} status-${escapeAttr(job.status)}" data-select-type="job" data-select-id="${escapeAttr(job.id)}">
      <span class="status-pill ${toneFor(job.status)}"><span class="status-dot"></span>${escapeHtml(job.status)}</span>
      <strong>${escapeHtml(shortJobName(job.id, job.taskId))}</strong>
      <small>${escapeHtml(job.agentId || job.role || "unassigned")}</small>
    </button>
  `;
}

function renderAgents() {
  const agents = filteredAgents();
  els.agents.innerHTML = `
    <div class="agent-grid">
      ${agents.length ? agents.map(renderAgentCard).join("") : `<div class="empty-note">No matching agents</div>`}
    </div>
  `;
  bindSelectionButtons(els.agents);
}

function renderAgentCard(agent) {
  const selected = isSelected("agent", agent.id) ? "selected" : "";
  return `
    <button class="agent-card ${selected}" data-select-type="agent" data-select-id="${escapeAttr(agent.id)}">
      <div class="agent-top">
        <div class="avatar">${escapeHtml(initials(agent.name))}</div>
        <div>
          <h2>${escapeHtml(agent.name)}</h2>
          <p>${escapeHtml(agent.role || "unassigned")} ${agent.active ? "- active" : ""}</p>
        </div>
      </div>
      <div class="chip-row">
        <span class="chip ${agent.active ? "tone-blue" : ""}">${escapeHtml(agent.currentJob || "idle")}</span>
        ${agent.engine ? `<span class="chip">${escapeHtml(agent.engine)}</span>` : ""}
      </div>
      <div class="agent-stats">
        <div class="agent-stat"><strong>${agent.jobCount}</strong><span>Jobs</span></div>
        <div class="agent-stat"><strong>${agent.doneJobs}</strong><span>Done</span></div>
        <div class="agent-stat"><strong>${agent.sessionCount}</strong><span>Sessions</span></div>
      </div>
    </button>
  `;
}

function renderChat() {
  const agent = consoleAgent();
  const inputAllowed = agentInputAllowed();
  const inputReady = Boolean(inputAllowed && agent?.interactive && agent?.inputReady);
  const isBusy = Boolean(agent?.busy);
  const statusLabel = agent ? (!inputAllowed ? "observing" : isBusy ? "busy" : inputReady ? "ready" : "starting") : "offline";
  const statusTone = !inputAllowed ? "" : isBusy ? "tone-blue" : inputReady ? "tone-green" : "tone-amber";
  const placeholder = inputReady ? "Message console" : "Console agent is not ready";
  const existingInput = document.querySelector("#chatInput");
  if (existingInput) state.chatDraft = existingInput.value;
  const actionDisabled = !inputReady || !state.chatDraft.trim();
  const steerHidden = isBusy ? "" : "hidden";
  const steerDisabled = inputReady ? "" : "disabled";

  els.chat.innerHTML = `
    <section class="chat-shell" aria-label="Console chat">
      <div class="chat-heading">
        <div>
          <p class="eyebrow">Console</p>
          <h2>Assistant</h2>
        </div>
        <span class="status-pill ${statusTone}" id="chatStatus"><span class="status-dot"></span>${escapeHtml(statusLabel)}</span>
      </div>
      <div class="chat-thread" id="chatThread" aria-live="polite">
        <div class="empty-note">Loading chat</div>
        <div class="chat-bottom-sentinel" id="chatBottomSentinel" aria-hidden="true"></div>
      </div>
      ${inputAllowed ? `<form class="chat-composer" id="chatComposer">
        <textarea id="chatInput" rows="1" placeholder="${escapeAttr(placeholder)}" ${inputReady ? "" : "disabled"}>${escapeHtml(state.chatDraft)}</textarea>
        <div class="chat-actions">
          <button class="chat-action-button send" type="submit" id="chatActionButton" ${actionDisabled ? "disabled" : ""}>Send</button>
          <button class="chat-steer-button" type="button" id="chatSteerButton" title="Steer now" aria-label="Steer now" ${steerHidden} ${steerDisabled}>
            <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
              <path d="M8 3h8l5 5v8l-5 5H8l-5-5V8zM9.2 8.7v6.6h5.6V8.7z"/>
            </svg>
          </button>
        </div>
        <div class="chat-input-hint">Enter to send, Shift+Enter for new line</div>
      </form>` : observationOnlyNotice()}
    </section>
  `;

  bindChatComposer(agent);
  bindChatThread();
  state.chatWasBusy = isBusy;
  startChatTimer();
  refreshChatTranscript({ scroll: true });

  const input = document.querySelector("#chatInput");
  autoSizeChatInput(input);
  if (inputReady && input && !state.chatAutofocused) {
    input.focus({ preventScroll: true });
    state.chatAutofocused = true;
  }
}

function bindChatComposer(agent) {
  const form = document.querySelector("#chatComposer");
  const input = document.querySelector("#chatInput");
  const steerButton = document.querySelector("#chatSteerButton");
  if (!form || !input) return;

  input.addEventListener("input", () => {
    state.chatDraft = input.value;
    autoSizeChatInput(input);
    syncChatAction(consoleAgent() || agent);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitChatAction(consoleAgent() || agent, input);
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitChatAction(consoleAgent() || agent, input);
  });
  steerButton?.addEventListener("click", () => sendChatSteer(consoleAgent() || agent, input));
  syncChatAction(agent);
}

function bindChatThread() {
  const thread = document.querySelector("#chatThread");
  if (!thread) return;
  thread.addEventListener("scroll", () => {
    state.chatPinned = isNearThreadBottom(thread);
  }, { passive: true });
}

function syncChatAction(agent) {
  const input = document.querySelector("#chatInput");
  const actionButton = document.querySelector("#chatActionButton");
  const steerButton = document.querySelector("#chatSteerButton");
  if (!input || !actionButton) return;
  if (!agentInputAllowed()) {
    input.disabled = true;
    actionButton.disabled = true;
    if (steerButton) steerButton.hidden = true;
    return;
  }
  const isBusy = Boolean(agent?.busy);
  actionButton.textContent = "Send";
  actionButton.classList.add("send");
  actionButton.classList.remove("stop");
  actionButton.disabled = !input.value.trim() || !agent?.inputReady;
  if (steerButton) {
    steerButton.hidden = !isBusy;
    steerButton.disabled = !agent?.inputReady || !isBusy;
  }
}

function submitChatAction(agent, input) {
  sendChatPrompt(agent, input);
}

function sendChatSteer(agent, input) {
  if (!agent?.inputReady) {
    toast("Console input is not ready");
    return;
  }
  const customMessage = input?.value.trim() || "";
  const message = customMessage || "Stop.";
  const hasCustomMessage = Boolean(customMessage);
  sendAgentInput(agent.id, message, "steer", {
    input,
    clearInput: hasCustomMessage,
    refreshInspector: false,
    refreshChat: true,
    successMessage: hasCustomMessage ? "Steer sent" : "Stop sent"
  }).then((sent) => {
    if (sent && hasCustomMessage) {
      state.chatDraft = "";
      autoSizeChatInput(input);
    }
  });
}

function sendChatPrompt(agent, input) {
  if (!agent?.inputReady) {
    toast("Console input is not ready");
    return;
  }
  const mode = agent?.busy ? "follow_up" : "prompt";
  sendAgentInput(agent.id, input.value, mode, {
    input,
    clearInput: true,
    refreshInspector: false,
    refreshChat: true,
    successMessage: agent?.busy ? "Follow-up queued" : "Message sent"
  }).then((sent) => {
    if (sent) {
      state.chatDraft = "";
      autoSizeChatInput(input);
      agent.busy = true;
      state.chatPinned = true;
      updateChatAgentState(agent);
    }
  });
}

async function refreshChatTranscript(options = {}) {
  const { scroll = false } = options;
  const thread = document.querySelector("#chatThread");
  if (!thread || state.view !== "chat") return;
  if (state.chatTranscriptInFlight) return;

  const agent = consoleAgent();
  if (!agent) {
    thread.innerHTML = `<div class="empty-note">Console agent is not running</div>`;
    return;
  }

  state.chatTranscriptInFlight = true;
  try {
    const response = await fetch(`/api/file?type=agent&id=${encodeURIComponent(agent.id)}&file=transcript.log&tail=1&maxBytes=${CHAT_TRANSCRIPT_MAX_BYTES}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "transcript load failed");
    const renderKey = `${payload.size || 0}:${payload.text?.length || 0}`;
    const isNewContent = thread.dataset.renderKey !== renderKey;
    const stickToBottom = scroll || state.chatPinned || isNearThreadBottom(thread);
    if (isNewContent) {
      const messages = visibleChatMessages(parseTranscriptMessages(payload.text || ""));
      updateChatMessages(thread, messages);
      thread.dataset.renderKey = renderKey;
    }
    if (stickToBottom) scrollChatToBottom(thread);
  } catch (error) {
    updateChatError(thread, error.message);
  } finally {
    state.chatTranscriptInFlight = false;
  }
}

async function refreshChatState() {
  if (state.view !== "chat") return;
  if (state.chatStateInFlight) return;
  state.chatStateInFlight = true;
  try {
    const response = await fetch("/api/snapshot");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "snapshot failed");
    const previousDraft = document.querySelector("#chatInput")?.value ?? state.chatDraft;
    state.snapshot = payload;
    state.root = payload.root;
    setRootDisplay(payload.root);
    const agent = consoleAgent();
    state.chatDraft = previousDraft;
    updateChatAgentState(agent);
    state.chatStateLastRefresh = Date.now();
  } catch (error) {
    els.railStatus.classList.add("error");
  } finally {
    state.chatStateInFlight = false;
  }
}

function parseTranscriptMessages(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  const messages = [];
  let current = null;
  const fallback = [];

  const flush = () => {
    if (!current) return;
    const body = current.lines.join("\n").trim();
    if (body) messages.push({ role: current.role, title: current.title, text: body });
    current = null;
  };

  for (const line of lines) {
    const title = transcriptSectionTitle(line);
    if (title) {
      flush();
      current = { role: chatRoleFor(title), title, lines: [] };
      continue;
    }
    if (!current) {
      if (line.trim() && !line.includes("earlier content truncated")) fallback.push(line);
      continue;
    }
    if (/^\[\d{4}-\d{2}-\d{2}T/.test(line)) {
      flush();
      continue;
    }
    current.lines.push(line);
  }
  flush();
  if (!messages.length) {
    const tail = fallback.join("\n").trim();
    if (tail) messages.push({ role: "tail", title: "Transcript Tail", text: tail });
  }
  return messages;
}

function transcriptSectionTitle(line) {
  const heading = line.match(/^###\s+(.+?)\s*$/);
  if (!heading) return "";
  const title = heading[1].trim();
  return TRANSCRIPT_SECTION_TITLES.has(title) ? title : "";
}

function chatRoleFor(title) {
  const key = title.toLowerCase();
  if (key === "user") return "user";
  if (key === "assistant") return "assistant";
  return "trace";
}

function renderChatMessages(messages) {
  if (!messages.length) return `<div class="empty-note">No console messages yet</div>`;
  return messages.map((message, index) => renderChatMessage(message, chatMessageKey(message, index))).join("");
}

function visibleChatMessages(messages) {
  return messages.slice(-CHAT_MAX_MESSAGES);
}

function updateChatMessages(thread, messages) {
  if (!messages.length) {
    if (!thread.querySelector(".empty-note") || thread.querySelector("[data-chat-key]")) {
      thread.innerHTML = `<div class="empty-note">No console messages yet</div>${chatBottomSentinelHtml()}`;
    } else {
      ensureChatBottomSentinel(thread);
    }
    state.chatMessageKeys = [];
    return;
  }

  const keys = messages.map(chatMessageKey);
  const signatures = messages.map(chatMessageSignature);
  ensureChatBottomSentinel(thread);
  thread.querySelectorAll(".empty-note").forEach((node) => node.remove());

  const nodes = Array.from(thread.querySelectorAll("[data-chat-key]"));
  const commonLength = Math.min(nodes.length, keys.length);
  const prefixMatches = nodes.slice(0, commonLength).every((node, index) => node.dataset.chatKey === keys[index]);

  if (!prefixMatches) {
    thread.innerHTML = `${renderChatMessages(messages)}${chatBottomSentinelHtml()}`;
    state.chatMessageKeys = keys;
    return;
  }

  messages.slice(0, commonLength).forEach((message, index) => {
    const node = nodes[index];
    if (node.dataset.chatSig === signatures[index]) return;
    const replacement = htmlToElement(renderChatMessage(message, keys[index], signatures[index]));
    node.replaceWith(replacement);
  });

  nodes.slice(keys.length).forEach((node) => node.remove());

  const sentinel = ensureChatBottomSentinel(thread);
  messages.slice(nodes.length).forEach((message, offset) => {
    const index = nodes.length + offset;
    sentinel.insertAdjacentElement("beforebegin", htmlToElement(renderChatMessage(message, keys[index], signatures[index])));
  });

  state.chatMessageKeys = keys;
}

function updateChatError(thread, message) {
  thread.innerHTML = `<div class="empty-note">${escapeHtml(message)}</div>${chatBottomSentinelHtml()}`;
  state.chatMessageKeys = [];
}

function renderChatMessage(message, key = chatMessageKey(message, 0), signature = chatMessageSignature(message)) {
  const attrs = `data-chat-key="${escapeAttr(key)}" data-chat-sig="${escapeAttr(signature)}"`;
  const renderText = chatRenderableText(message.text);
  const truncation = renderText.truncated
    ? `<p class="chat-truncation">Showing the latest part of a very large message.</p>`
    : "";
  if (message.role === "tail") {
    return `
      <article class="chat-message assistant chat-tail" ${attrs}>
        <div class="chat-bubble">
          ${truncation || `<p class="chat-truncation">Showing recent transcript tail.</p>`}
          <pre><code>${escapeHtml(renderText.text)}</code></pre>
        </div>
      </article>
    `;
  }
  if (message.role === "trace") {
    const traceBody = renderText.truncated
      ? `<pre><code>${escapeHtml(renderText.text)}</code></pre>`
      : markdownToHtml(renderText.text);
    return `
      <details class="chat-trace" ${attrs}>
        <summary>${escapeHtml(message.title)}</summary>
        <div>${truncation}${traceBody}</div>
      </details>
    `;
  }

  const isUser = message.role === "user";
  const body = isUser
    ? plainTextToHtml(renderText.text)
    : renderText.truncated
      ? `<pre><code>${escapeHtml(renderText.text)}</code></pre>`
      : markdownToHtml(renderText.text);
  return `
    <article class="chat-message ${isUser ? "user" : "assistant"}" ${attrs}>
      <div class="chat-bubble">
        ${truncation}${body}
      </div>
    </article>
  `;
}

function chatMessageKey(message, index) {
  return `${index}:${message.role}:${message.title}`;
}

function chatMessageSignature(message) {
  const text = String(message.text || "");
  return `${text.length}:${hashString(text)}`;
}

function chatRenderableText(text) {
  const value = String(text || "");
  if (value.length <= CHAT_RENDER_TEXT_LIMIT) return { text: value, truncated: false };
  return {
    text: value.slice(-CHAT_RENDER_TEXT_LIMIT),
    truncated: true
  };
}

function hashString(value) {
  let hash = 2166136261;
  const text = String(value || "");
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function htmlToElement(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

function chatBottomSentinelHtml() {
  return `<div class="chat-bottom-sentinel" id="chatBottomSentinel" aria-hidden="true"></div>`;
}

function ensureChatBottomSentinel(thread) {
  let sentinel = thread.querySelector("#chatBottomSentinel");
  if (!sentinel) {
    thread.insertAdjacentHTML("beforeend", chatBottomSentinelHtml());
    sentinel = thread.querySelector("#chatBottomSentinel");
  }
  return sentinel;
}

function scrollChatToBottom(thread) {
  ensureChatBottomSentinel(thread);
  thread.scrollLeft = 0;
  thread.scrollTop = thread.scrollHeight;
  state.chatPinned = true;
}

function updateChatAgentState(agent) {
  const inputAllowed = agentInputAllowed();
  const inputReady = Boolean(inputAllowed && agent?.interactive && agent?.inputReady);
  const isBusy = Boolean(agent?.busy);
  const status = document.querySelector("#chatStatus");
  const input = document.querySelector("#chatInput");

  if (status) {
    const label = agent ? (!inputAllowed ? "observing" : isBusy ? "busy" : inputReady ? "ready" : "starting") : "offline";
    const tone = !inputAllowed ? "" : isBusy ? "tone-blue" : inputReady ? "tone-green" : "tone-amber";
    status.className = `status-pill ${tone}`;
    status.innerHTML = `<span class="status-dot"></span>${escapeHtml(label)}`;
  }

  if (input) {
    input.disabled = !inputReady;
    input.placeholder = inputReady ? "Message console" : "Console agent is not ready";
  }

  state.chatWasBusy = isBusy;
  syncChatAction(agent);
}

function isNearThreadBottom(thread) {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 96;
}

function startChatTimer() {
  stopChatTimer();
  state.chatTimer = setInterval(() => {
    refreshChatTranscript();
    if (Date.now() - state.chatStateLastRefresh >= CHAT_STATE_REFRESH_MS) {
      refreshChatState();
    }
  }, CHAT_REFRESH_MS);
}

function stopChatTimer() {
  if (state.chatTimer) {
    clearInterval(state.chatTimer);
    state.chatTimer = null;
  }
}

function autoSizeChatInput(input) {
  if (!input) return;
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 172)}px`;
}

function consoleAgent() {
  return (
    state.snapshot?.agents.find((agent) => agent.id === "console") ||
    state.snapshot?.agents.find((agent) => agent.role === "console" && agent.interactive) ||
    null
  );
}

function agentInputAllowed(snapshot = state.snapshot) {
  return snapshot?.capabilities?.agentInput !== false;
}

function observationOnlyNotice() {
  return `<div class="empty-note" role="status">Observation-only viewer. Sending and steering are disabled.</div>`;
}

function renderMap() {
  const tasks = filteredTasks().slice(0, 18);
  const taskIds = new Set(tasks.map((task) => task.id));
  const jobs = state.snapshot.jobs.filter((job) => taskIds.has(job.taskId)).slice(0, 70);
  const agentIds = new Set(jobs.map((job) => job.agentId).filter(Boolean));
  const agents = state.snapshot.agents.filter((agent) => agentIds.has(agent.id));
  const graph = layoutGraph(tasks, jobs, agents);

  els.map.innerHTML = `
    <section class="map-panel">
      <div class="section-head">
        <h2>Work Map</h2>
        <span class="count-pill">${tasks.length} tasks - ${jobs.length} jobs - ${agents.length} agents</span>
      </div>
      <div class="graph-wrap">
        <svg class="graph-svg" style="height:${graph.height}px" viewBox="0 0 1200 ${graph.height}" preserveAspectRatio="xMinYMin meet" role="img" aria-label="Agent task and job relationship map">
          ${graph.edges.map(renderEdge).join("")}
          ${graph.nodes.map(renderGraphNode).join("")}
        </svg>
      </div>
    </section>
  `;
  bindSelectionButtons(els.map);
}

function layoutGraph(tasks, jobs, agents) {
  const nodes = [];
  const edges = [];
  const jobsByTask = groupBy(jobs, (job) => job.taskId);
  const taskGap = 34;
  const jobGap = 10;
  const top = 38;
  let cursor = top;
  const jobPositions = new Map();
  const taskPositions = new Map();
  const agentPositions = new Map();

  tasks.forEach((task, index) => {
    const taskJobs = jobsByTask.get(task.id) || [];
    const groupHeight = Math.max(58, taskJobs.length * (42 + jobGap) - jobGap);
    const taskNode = { type: "task", id: task.id, title: task.title, sub: task.flowState, x: 70, y: cursor + Math.max(0, (groupHeight - 48) / 2), w: 230, h: 48 };
    taskPositions.set(task.id, taskNode);
    nodes.push(taskNode);

    taskJobs.forEach((job, jobIndex) => {
      const jobNode = { type: "job", id: job.id, title: shortJobName(job.id, job.taskId), sub: `${job.stage} - ${job.status}`, x: 470, y: cursor + jobIndex * (42 + jobGap), w: 250, h: 42 };
      jobPositions.set(job.id, jobNode);
      nodes.push(jobNode);
    });

    cursor += groupHeight + taskGap;
  });

  const height = Math.max(660, cursor + top);
  const agentY = spaceY(Math.max(agents.length, 1), 90, Math.max(90, height - 90));

  agents.forEach((agent, index) => {
    const agentNode = { type: "agent", id: agent.id, title: agent.name, sub: agent.role || "agent", x: 900, y: agentY(index), w: 210, h: 48 };
    agentPositions.set(agent.id, agentNode);
    nodes.push(agentNode);
  });

  jobs.forEach((job) => {
    const task = taskPositions.get(job.taskId);
    const jobPos = jobPositions.get(job.id);
    if (task && jobPos) edges.push(edgeBetween(task, jobPos));
    const agent = agentPositions.get(job.agentId);
    if (agent && jobPos) edges.push(edgeBetween(jobPos, agent));
  });

  return { nodes, edges, height };
}

function edgePath(from, to) {
  const x1 = from.x + from.w;
  const y1 = from.y + from.h / 2;
  const x2 = to.x;
  const y2 = to.y + to.h / 2;
  const mid = (x1 + x2) / 2;
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

function edgeBetween(from, to) {
  return {
    path: edgePath(from, to),
    fromType: from.type,
    fromId: from.id,
    toType: to.type,
    toId: to.id
  };
}

function renderEdge(edge) {
  const selected = isEdgeSelected(edge) ? " selected" : "";
  return `<path class="graph-edge${selected}" d="${escapeAttr(edge.path)}" fill="none"></path>`;
}

function isEdgeSelected(edge) {
  if (!state.selected) return false;
  const { type, id } = state.selected;
  if (type !== "job") return false;
  return (edge.fromType === "job" && edge.fromId === id) || (edge.toType === "job" && edge.toId === id);
}

function renderGraphNode(node) {
  return `
    <g class="graph-node" tabindex="0" data-select-type="${escapeAttr(node.type)}" data-select-id="${escapeAttr(node.id)}">
      <rect x="${node.x}" y="${node.y}" width="${node.w}" height="${node.h}" rx="8"></rect>
      <text x="${node.x + 12}" y="${node.y + 20}">${escapeHtml(fitText(node.title, 28))}</text>
      <text class="graph-sub" x="${node.x + 12}" y="${node.y + 36}">${escapeHtml(fitText(node.sub, 32))}</text>
    </g>
  `;
}

function renderInspector() {
  if (!state.selected) {
    els.inspector.classList.remove("open");
    els.inspector.innerHTML = `
      <div class="inspector-empty">
        <div class="empty-symbol">AW</div>
        <h2>No Selection</h2>
      </div>
    `;
    return;
  }

  els.inspector.classList.add("open");
  const { type, item } = state.selected;
  if (type === "task") renderTaskInspector(item);
  if (type === "job") renderJobInspector(item);
  if (type === "agent") renderAgentInspector(item);
}

function renderTaskInspector(task) {
  const currentFile = activeFileFor([
    { label: "Log", type: "task", id: task.id, file: "log.md", tail: true },
    { label: "Spec", type: "task", id: task.id, file: "spec.md", tail: false },
    ...(task.hasResult ? [{ label: "Result", type: "task", id: task.id, file: "result.md", tail: false }] : [])
  ]);
  els.inspector.innerHTML = `
    ${inspectorHead(task.title, "task", task.id)}
    <div class="chip-row">
      <span class="status-pill status-${escapeAttr(task.flowState)} ${toneFor(task.flowState)}"><span class="status-dot"></span>${escapeHtml(task.flowState)}</span>
      <span class="chip">${task.progress}% complete</span>
    </div>
    <div class="detail-grid">
      ${detail("Jobs", `${task.doneJobs}/${task.jobCount}`)}
      ${detail("Agents", task.agents.length || "none")}
      ${detail("Updated", formatDateTime(task.updatedAt))}
      ${detail("State", task.state)}
    </div>
    <div class="tabs">
      ${currentFile.options.map((option) => renderFileTab(option, currentFile.active)).join("")}
      <button class="tab-button live-button ${state.live ? "active" : ""}" id="liveFileButton" title="Refresh current file every 2 seconds">Live</button>
    </div>
    <div id="filePane" class="markdown-render">${markdownToHtml(task.objective || task.specPreview)}</div>
    <h3>Jobs</h3>
    <div class="log-card">
      ${task.jobs.length ? task.jobs.map((id) => {
        const job = lookupSelection("job", id);
        return `<button class="pipeline-job status-${escapeAttr(job.status)}" data-select-type="job" data-select-id="${escapeAttr(id)}"><span class="status-pill ${toneFor(job.status)}"><span class="status-dot"></span>${escapeHtml(job.status)}</span><strong>${escapeHtml(shortJobName(id, task.id))}</strong><small>${escapeHtml(job.agentId || job.role)}</small></button>`;
      }).join("") : "No jobs"}
    </div>
  `;
  bindInspector();
  loadFile(currentFile.active.type, currentFile.active.id, currentFile.active.file, currentFile.active.tail);
}

function renderJobInspector(job) {
  const currentFile = activeFileFor([
    { label: "Log", type: "job", id: job.id, file: "log.md", tail: true },
    { label: "Spec", type: "job", id: job.id, file: "spec.md", tail: false },
    ...(job.agentId ? [
      { label: "Transcript", type: "agent", id: job.agentId, file: "transcript.log", tail: true },
      { label: "Agent Errors", type: "agent", id: job.agentId, file: "error.log", tail: true }
    ] : [])
  ]);
  els.inspector.innerHTML = `
    ${inspectorHead(shortJobName(job.id, job.taskId), "job", job.id)}
    <div class="chip-row">
      <span class="status-pill status-${escapeAttr(job.status)} ${toneFor(job.status)}"><span class="status-dot"></span>${escapeHtml(job.status)}</span>
      <span class="chip">${escapeHtml(job.stage)}</span>
      <span class="chip">${escapeHtml(job.role || "no role")}</span>
    </div>
    <div class="detail-grid">
      ${detail("Task", job.taskId)}
      ${detail("Agent", job.agentId || "unassigned")}
      ${detail("Updated", formatDateTime(job.updatedAt))}
      ${detail("Events", job.eventCount)}
    </div>
    <div class="tabs">
      ${currentFile.options.map((option) => renderFileTab(option, currentFile.active)).join("")}
      <button class="tab-button live-button ${state.live ? "active" : ""}" id="liveFileButton" title="Refresh current file every 2 seconds">Live</button>
    </div>
    <div id="filePane" class="markdown-render">${markdownToHtml(job.latestSummary || "Select Log to load full content.")}</div>
  `;
  bindInspector();
  loadFile(currentFile.active.type, currentFile.active.id, currentFile.active.file, currentFile.active.tail);
}

function renderAgentInspector(agent) {
  const currentFile = activeFileFor([
    { label: "Transcript", type: "agent", id: agent.id, file: "transcript.log", tail: true },
    { label: "Errors", type: "agent", id: agent.id, file: "error.log", tail: true },
    { label: "Prompt", type: "agent", id: agent.id, file: "prompt.md", tail: false }
  ]);
  els.inspector.innerHTML = `
    ${inspectorHead(agent.name, "agent", agent.id)}
    <div class="chip-row">
      <span class="status-pill ${agent.active ? "tone-blue" : ""}"><span class="status-dot"></span>${escapeHtml(agent.active ? "active" : "idle")}</span>
      <span class="chip">${escapeHtml(agent.role || "no role")}</span>
      ${agent.engine ? `<span class="chip">${escapeHtml(agent.engine)}</span>` : ""}
    </div>
    <div class="detail-grid">
      ${detail("Current job", agent.currentJob || "none")}
      ${detail("Jobs", agent.jobCount)}
      ${detail("Sessions", agent.sessionCount)}
      ${detail("Last start", formatDateTime(agent.lastStartedAt))}
    </div>
    <div class="tabs">
      ${currentFile.options.map((option) => renderFileTab(option, currentFile.active)).join("")}
      <button class="tab-button live-button ${state.live ? "active" : ""}" id="liveFileButton" title="Refresh current file every 2 seconds">Live</button>
    </div>
    <div id="filePane" class="markdown-render">${markdownToHtml(agent.promptPreview || "Select a file to load.")}</div>
    ${agent.interactive ? renderInteractiveComposer(agent) : ""}
  `;
  bindInspector();
  bindInteractiveComposer(agent);
  loadFile(currentFile.active.type, currentFile.active.id, currentFile.active.file, currentFile.active.tail);
}

function renderInteractiveComposer(agent, inputAllowed = agentInputAllowed()) {
  if (!inputAllowed) return observationOnlyNotice();
  return `
    <form class="agent-composer" id="agentComposer" data-agent-id="${escapeAttr(agent.id)}">
      <textarea id="agentMessageInput" rows="3" placeholder="Message ${escapeAttr(agent.name)}" ${agent.inputReady ? "" : "disabled"}></textarea>
      <div class="composer-actions">
        <button class="tab-button" type="button" id="agentSteerButton" ${agent.inputReady ? "" : "disabled"}>Steer</button>
        <button class="tab-button active" type="submit" ${agent.inputReady ? "" : "disabled"}>Send</button>
      </div>
    </form>
  `;
}

function activeFileFor(options) {
  const active = options.find((option) => sameFile(option, state.activeFile)) || options[0];
  return { active, options };
}

function sameFile(left, right) {
  return Boolean(right && left.type === right.type && left.id === right.id && left.file === right.file);
}

function renderFileTab(option, active) {
  const isActive = sameFile(option, active);
  return `
    <button class="tab-button ${isActive ? "active" : ""}" data-file-type="${escapeAttr(option.type)}" data-file-id="${escapeAttr(option.id)}" data-file-name="${escapeAttr(option.file)}" data-tail="${option.tail ? "1" : "0"}">
      ${escapeHtml(option.label)}
    </button>
  `;
}

function inspectorHead(title, type, id) {
  return `
    <div class="inspector-head">
      <div>
        <p class="eyebrow">${escapeHtml(type)}</p>
        <h2>${escapeHtml(title)}</h2>
        <div class="time">${escapeHtml(id)}</div>
      </div>
      <button class="icon-button close-button" id="closeInspector" title="Close" aria-label="Close">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 10.6 4.95-4.95 1.4 1.4L13.4 12l4.95 4.95-1.4 1.4L12 13.4l-4.95 4.95-1.4-1.4L10.6 12 5.65 7.05l1.4-1.4z"/></svg>
      </button>
    </div>
  `;
}

function detail(label, value) {
  return `<div class="detail"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "n/a")}</strong></div>`;
}

function bindInspector() {
  document.querySelector("#closeInspector")?.addEventListener("click", clearSelection);
  document.querySelector("#liveFileButton")?.addEventListener("click", () => setLive(!state.live));
  bindSelectionButtons(els.inspector);
  els.inspector.querySelectorAll("[data-file-name]").forEach((button) => {
    button.addEventListener("click", () => {
      els.inspector.querySelectorAll(".tab-button").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      document.querySelector("#liveFileButton")?.classList.toggle("active", state.live);
      loadFile(button.dataset.fileType, button.dataset.fileId, button.dataset.fileName, button.dataset.tail === "1");
    });
  });
}

function bindInteractiveComposer(agent) {
  const form = document.querySelector("#agentComposer");
  const input = document.querySelector("#agentMessageInput");
  const steerButton = document.querySelector("#agentSteerButton");
  if (!form || !input || !agent?.interactive || !agentInputAllowed()) return;

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendAgentInput(agent.id, input.value, "prompt");
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendAgentInput(agent.id, input.value, "prompt");
  });
  steerButton?.addEventListener("click", () => sendAgentInput(agent.id, input.value, "steer"));
}

async function sendAgentInput(agentId, value, mode, options = {}) {
  const {
    input = document.querySelector("#agentMessageInput"),
    clearInput = true,
    refreshInspector = true,
    refreshChat = state.view === "chat",
    successMessage = mode === "steer" ? "Steer sent" : "Message sent"
  } = options;
  if (!agentInputAllowed()) {
    toast("Observation-only viewer; agent input is disabled");
    return false;
  }
  const message = String(value || "").trim();
  if (!message) return false;
  try {
    const response = await fetch(`/api/agent-input?id=${encodeURIComponent(agentId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, mode })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "send failed");
    if (clearInput && input) input.value = "";
    toast(successMessage);
    if (refreshInspector) {
      const transcript = { type: "agent", id: agentId, file: "transcript.log", tail: true };
      loadFile(transcript.type, transcript.id, transcript.file, transcript.tail, { silent: true });
      if (!state.live) setLive(true);
    }
    if (refreshChat) refreshChatTranscript({ scroll: true });
    return true;
  } catch (error) {
    toast(error.message);
    return false;
  }
}

async function loadFile(type, id, file, tail = false, options = {}) {
  const { silent = false } = options;
  const pane = document.querySelector("#filePane");
  if (!pane) return;
  state.activeFile = { type, id, file, tail };
  if (!silent) pane.textContent = "Loading...";
  const renderMarkdown = isMarkdownFile(file);
  pane.className = renderMarkdown ? "markdown-render" : "preflight";
  try {
    const response = await fetch(`/api/file?type=${encodeURIComponent(type)}&id=${encodeURIComponent(id)}&file=${encodeURIComponent(file)}&tail=${tail ? "1" : "0"}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "file load failed");
    if (renderMarkdown) {
      pane.innerHTML = markdownToHtml(payload.text || "(empty)");
    } else {
      pane.textContent = payload.text || "(empty)";
    }
    if (tail && !renderMarkdown) pane.scrollTop = pane.scrollHeight;
    if (payload.truncated) toast(`Showing ${tail ? "tail of" : "first part of"} ${file}`);
  } catch (error) {
    pane.textContent = error.message;
  }
}

function setLive(enabled) {
  state.live = enabled;
  document.querySelector("#liveFileButton")?.classList.toggle("active", enabled);
  stopLiveTimer();
  if (enabled) {
    refreshActiveFile();
    state.liveTimer = setInterval(refreshActiveFile, LIVE_REFRESH_MS);
    toast("Live file refresh on");
  } else {
    toast("Live file refresh off");
  }
}

function stopLive() {
  state.live = false;
  stopLiveTimer();
}

function stopLiveTimer() {
  if (state.liveTimer) {
    clearInterval(state.liveTimer);
    state.liveTimer = null;
  }
}

function refreshActiveFile() {
  if (!state.activeFile) return;
  const { type, id, file, tail } = state.activeFile;
  const liveTail = tail || file.endsWith(".log") || file === "log.md";
  loadFile(type, id, file, liveTail, { silent: true });
}

function isMarkdownFile(file) {
  return file.endsWith(".md");
}

function markdownToHtml(markdown) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^```(\w+)?\s*$/);
    if (fence) {
      const code = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      index += index < lines.length ? 1 : 0;
      html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    if (isTableStart(lines, index)) {
      const table = [lines[index], lines[index + 1]];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        table.push(lines[index]);
        index += 1;
      }
      html.push(renderMarkdownTable(table));
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      html.push(`<h${level}>${inlineMarkdown(heading[2].trim())}</h${level}>`);
      index += 1;
      continue;
    }

    const quote = line.match(/^>\s?(.*)$/);
    if (quote) {
      const body = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        body.push(lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      html.push(`<blockquote>${markdownToHtml(body.join("\n"))}</blockquote>`);
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*[-*]\s+(.+)$/);
        if (!item) break;
        items.push(`<li>${inlineMarkdown(item[1])}</li>`);
        index += 1;
      }
      html.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (ordered) {
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*\d+\.\s+(.+)$/);
        if (!item) break;
        items.push(`<li>${inlineMarkdown(item[1])}</li>`);
        index += 1;
      }
      html.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    const paragraph = [line.trim()];
    index += 1;
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      !/^```/.test(lines[index]) &&
      !/^\s*[-*]\s+/.test(lines[index]) &&
      !/^\s*\d+\.\s+/.test(lines[index]) &&
      !/^>\s?/.test(lines[index]) &&
      !isTableStart(lines, index)
    ) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    html.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
  }

  return html.join("");
}

function plainTextToHtml(text) {
  return escapeHtml(text).replace(/\n/g, "<br>");
}

function inlineMarkdown(value) {
  let text = escapeHtml(value);
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label, href) => {
    const safeHref = safeLinkHref(href);
    return safeHref ? `<a href="${safeHref}" target="_blank" rel="noreferrer">${label}</a>` : label;
  });
  return text;
}

function safeLinkHref(href) {
  const value = String(href || "").trim();
  if (/^(https?:|mailto:|#|\/)/i.test(value)) {
    return escapeAttr(value);
  }
  return "";
}

function isTableStart(lines, index) {
  return (
    index + 1 < lines.length &&
    lines[index].includes("|") &&
    /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1])
  );
}

function renderMarkdownTable(lines) {
  const rows = lines.map(splitTableRow);
  const head = rows[0] || [];
  const body = rows.slice(2);
  return `
    <div class="table-wrap">
      <table>
        <thead><tr>${head.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead>
        <tbody>${body.map((row) => `<tr>${row.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`).join("")}</tbody>
      </table>
    </div>
  `;
}

function splitTableRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function bindSelectionButtons(root) {
  root.querySelectorAll("[data-select-type]").forEach((node) => {
    node.addEventListener("click", (event) => {
      event.preventDefault();
      selectItem(node.dataset.selectType, node.dataset.selectId);
    });
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectItem(node.dataset.selectType, node.dataset.selectId);
      }
    });
  });
}

function selectItem(type, id) {
  const item = lookupSelection(type, id);
  if (!item) return;
  state.selected = { type, id, item };
  render();
}

function clearSelection() {
  stopLive();
  state.activeFile = null;
  state.selected = null;
  renderInspector();
  document.querySelectorAll(".selected").forEach((node) => node.classList.remove("selected"));
}

function lookupSelection(type, id) {
  const collection = {
    task: state.snapshot?.tasks,
    job: state.snapshot?.jobs,
    agent: state.snapshot?.agents
  }[type];
  return collection?.find((item) => item.id === id) || null;
}

function isSelected(type, id) {
  return state.selected?.type === type && state.selected?.id === id;
}

function filteredTasks() {
  return filterItems(state.snapshot.tasks, taskText);
}

function filteredJobs() {
  return filterItems(state.snapshot.jobs, jobText);
}

function filteredAgents() {
  return filterItems(state.snapshot.agents, agentText);
}

function filteredActivity() {
  return filterItems(state.snapshot.activity, (event) => `${event.type} ${event.itemId} ${event.taskId} ${event.title} ${event.body} ${event.role} ${event.agentId}`);
}

function filterItems(items, textFn) {
  if (!state.query) return items;
  return items.filter((item) => textFn(item).toLowerCase().includes(state.query));
}

function taskText(task) {
  return `${task.id} ${task.title} ${task.state} ${task.flowState} ${task.objective} ${task.roles.join(" ")} ${task.agents.join(" ")}`;
}

function jobText(job) {
  return `${job.id} ${job.taskId} ${job.status} ${job.role} ${job.agentId} ${job.stage} ${job.latestSummary}`;
}

function agentText(agent) {
  return `${agent.id} ${agent.name} ${agent.role} ${agent.engine} ${agent.currentJob} ${agent.promptPreview}`;
}

function groupBy(items, keyFn) {
  const groups = new Map();
  for (const item of items) {
    const key = keyFn(item);
    const group = groups.get(key) || [];
    group.push(item);
    groups.set(key, group);
  }
  return groups;
}

function shortJobName(id, taskId) {
  return id.startsWith(`${taskId}-`) ? id.slice(taskId.length + 1) : id;
}

function initials(name) {
  return String(name || "A")
    .split(/[-_\s]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() || "")
    .join("") || "A";
}

function toneFor(status) {
  if (["done", "closed"].includes(status)) return "tone-green";
  if (["failed", "blocked"].includes(status)) return "tone-coral";
  if (["running", "claimed", "active"].includes(status)) return "tone-blue";
  return "tone-amber";
}

function fitText(value, max) {
  const text = String(value || "");
  return text.length > max ? `${text.slice(0, max - 3)}...` : text;
}

function titleCase(value) {
  return String(value || "")
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function spaceY(count, top, bottom) {
  if (count <= 1) return () => (top + bottom) / 2;
  const step = (bottom - top) / (count - 1);
  return (index) => top + index * step;
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "now";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "n/a";
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.remove("visible"), 2200);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

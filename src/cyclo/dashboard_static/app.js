(() => {
  "use strict";

  const API_URL = "/api/snapshot";
  const API_VERSION = 4;
  const REFRESH_INTERVAL_MS = 10_000;
  const ACTIVITY_LIMIT = 10;
  const CARD_ACTIVITY_LIMIT = 3;

  const numberFormatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const dateFormatter = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });

  const elements = {
    connection: document.querySelector("#connection"),
    connectionLabel: document.querySelector("#connection-label"),
    refreshButton: document.querySelector("#refresh-button"),
    snapshotTime: document.querySelector("#snapshot-time"),
    refreshCountdown: document.querySelector("#refresh-countdown"),
    notice: document.querySelector("#notice"),
    noticeMessage: document.querySelector("#notice-message"),
    noticeDismiss: document.querySelector("#notice-dismiss"),
    loadingState: document.querySelector("#loading-state"),
    emptyState: document.querySelector("#empty-state"),
    emptyTitle: document.querySelector("#empty-title"),
    emptyMessage: document.querySelector("#empty-message"),
    clearFilters: document.querySelector("#clear-filters"),
    instanceGrid: document.querySelector("#instance-grid"),
    instanceTemplate: document.querySelector("#instance-card-template"),
    resultCount: document.querySelector("#result-count"),
    searchInput: document.querySelector("#search-input"),
    statusFilter: document.querySelector("#status-filter"),
    modeFilter: document.querySelector("#mode-filter"),
    sortSelect: document.querySelector("#sort-select"),
    autoRefresh: document.querySelector("#auto-refresh"),
    activityPanel: document.querySelector("#activity-panel"),
    activityStream: document.querySelector("#activity-stream"),
    statInstances: document.querySelector("#stat-instances"),
    statRunning: document.querySelector("#stat-running"),
    statTasks: document.querySelector("#stat-tasks"),
    statTasksDetail: document.querySelector("#stat-tasks-detail"),
    statAgents: document.querySelector("#stat-agents"),
    statAgentsDetail: document.querySelector("#stat-agents-detail"),
    statAttention: document.querySelector("#stat-attention"),
    statAttentionDetail: document.querySelector("#stat-attention-detail"),
    attentionStat: document.querySelector("#attention-stat"),
  };

  const state = {
    snapshot: null,
    loading: true,
    error: null,
    fetchedAt: null,
    nextRefreshAt: null,
    refreshTimer: null,
    activeRequest: null,
  };

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function asObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function asNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
  }

  function stringValue(value, fallback = "") {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
    return fallback;
  }

  function firstString(values, fallback = "") {
    for (const value of values) {
      const text = stringValue(value);
      if (text) return text;
    }
    return fallback;
  }

  function basename(value) {
    const clean = stringValue(value).replace(/[\\/]+$/, "");
    return clean.split(/[\\/]/).filter(Boolean).pop() || clean;
  }

  function safeDate(value) {
    if (!value) return null;
    const date = value instanceof Date ? value : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function displayContainerState(value) {
    if (value === "running") return "running";
    if (value === "restarting") return "starting";
    if (["absent", "stopped", "dead"].includes(value)) return "stopped";
    return "attention";
  }

  function normalizeContainerState(value) {
    const raw = stringValue(value, "unknown").toLowerCase();
    return [
      "absent",
      "running",
      "paused",
      "restarting",
      "stopped",
      "dead",
      "unknown",
    ].includes(raw) ? raw : "unknown";
  }

  function normalizeDesiredState(value) {
    const raw = stringValue(value).toLowerCase();
    if (["running", "stopped", "absent"].includes(raw)) return raw;
    return "unknown";
  }

  function normalizeReadiness(value) {
    const raw = stringValue(value).toLowerCase();
    if (["absent", "starting", "healthy", "unhealthy", "missing", "unknown"].includes(raw)) {
      return raw;
    }
    return "unknown";
  }

  function normalizeCounterGroup(value, keys) {
    const raw = asObject(value);
    const result = {};
    for (const key of keys) result[key] = asNumber(raw[key]);
    if (!result.total) {
      result.total = keys
        .filter((key) => key !== "total")
        .reduce((sum, key) => sum + result[key], 0);
    }
    return result;
  }

  function errorMessage(value) {
    if (typeof value === "string") return value;
    const error = asObject(value);
    return firstString([error.message, error.error, error.detail, error.title], "Unknown error");
  }

  function activityMessage(item, kind) {
    if (typeof item === "string") return item;
    const value = asObject(item);
    const direct = firstString([
      value.message,
      value.title,
      value.summary,
      value.description,
      value.action,
      value.subject,
    ]);
    if (direct) return direct;
    const identity = firstString([value.id, value.task_id, value.job_id]);
    const itemKind = firstString([value.kind], kind).toLowerCase();
    const status = firstString([value.status, value.state]);
    if (itemKind === "job" && identity) {
      const task = firstString([value.task_id]);
      return `Job ${identity}${status ? ` ${status}` : ""}${task ? ` · task ${task}` : ""}`;
    }
    if (itemKind === "task" && identity) {
      return `Task ${identity}${status ? ` ${status}` : ""}`;
    }
    return identity ? `${kind === "task" ? "Task" : "Activity"} ${identity}` : "Activity recorded";
  }

  function normalizeActivity(item, instance, kind = "activity") {
    const value = asObject(item);
    const timestamp = firstString([
      value.timestamp,
      value.updated_at,
      value.created_at,
      value.completed_at,
      value.time,
    ]);
    return {
      id: firstString([value.id, value.task_id, value.job_id]),
      instanceId: instance.id,
      team: instance.team,
      message: activityMessage(item, kind),
      actor: firstString([value.actor, value.agent, value.owner]),
      status: firstString([value.status, value.state, value.kind], kind),
      timestamp,
      time: safeDate(timestamp),
      kind,
    };
  }

  function normalizeInstance(rawValue, generatedAt) {
    const raw = asObject(rawValue);
    const id = stringValue(raw.id, "unknown-instance");
    const teamReference = stringValue(raw.team, id);
    const team = basename(teamReference) || id;
    const rawProject = asObject(raw.project);
    const project = firstString([
      rawProject.name,
      rawProject.path,
    ], "—");
    const projectReference = firstString([
      rawProject.definition,
      rawProject.path,
    ], project);
    const projectDescription = stringValue(rawProject.description);
    const normalizeProjectLocations = (values) => asArray(values).map((value) => {
      const mount = asObject(value);
      return {
        name: stringValue(mount.name),
        path: stringValue(mount.path),
        containerPath: stringValue(mount.container_path),
      };
    }).filter((mount) => mount.name && mount.containerPath);
    const workspaces = normalizeProjectLocations(rawProject.workspaces);
    const readOnlyMounts = normalizeProjectLocations(rawProject.read_only_mounts);
    const mode = asObject(raw.mode);
    const counts = asObject(raw.counts);
    const tasks = normalizeCounterGroup(counts.tasks, ["total", "open", "closed", "unknown"]);
    const jobs = normalizeCounterGroup(counts.jobs, [
      "total",
      "pending",
      "claimed",
      "running",
      "done",
      "failed",
      "unknown",
    ]);
    const agents = normalizeCounterGroup(counts.agents, ["total", "active"]);
    const errors = asArray(raw.errors).map(errorMessage).filter(Boolean);
    const container = normalizeContainerState(raw.container);
    const desired = normalizeDesiredState(raw.desired);
    const readiness = normalizeReadiness(raw.readiness);
    const operational = desired === "running"
      && container === "running"
      && readiness === "healthy";
    const settledStopped = desired === "stopped" && container === "absent";
    const runtimeState = displayContainerState(container);
    const rawHealth = asObject(raw.health);
    const healthState = firstString([rawHealth.state], operational ? "provider-unknown" : "inactive").toLowerCase();
    const knownHealthStates = [
      "ready",
      "provider-down",
      "provider-stale",
      "provider-unknown",
      "agents-attention",
      "agents-suspended",
      "agents-unknown",
      "inactive",
    ];
    const health = {
      state: knownHealthStates.includes(healthState) ? healthState : "provider-unknown",
      reason: firstString([rawHealth.reason], raw.health ? "" : "provider status unavailable"),
    };
    const needsAttention = !(operational || settledStopped)
      || runtimeState === "attention"
      || health.state.startsWith("provider-")
      || health.state.startsWith("agents-")
      || (health.state === "ready" && Boolean(health.reason))
      || errors.length > 0
      || tasks.unknown > 0
      || jobs.failed > 0
      || jobs.unknown > 0;
    const displayState = needsAttention ? "attention" : runtimeState;
    const unknownTasksReported = errors.some((error) =>
      /\btasks? (?:has|have) an unknown or unreadable state\b/i.test(error));
    const unknownJobsReported = errors.some((error) =>
      /\bjobs? (?:has|have) an unknown or unreadable status\b/i.test(error));
    const instance = {
      id,
      team,
      teamReference,
      project,
      projectReference,
      projectDescription,
      workspaces,
      readOnlyMounts,
      desired,
      container,
      readiness,
      operational,
      state: runtimeState,
      displayState,
      rawState: container,
      health,
      mode: {
        offline: Boolean(mode.offline),
        teamWrite: Boolean(mode.team_write),
      },
      generation: stringValue(raw.generation, "—"),
      agentwsUrl: agentwsUrlForCurrentHost(raw.agentws_port),
      tasks,
      jobs,
      agents,
      errors,
      recentActivity: [],
      recentTasks: [],
      generatedAt,
      failureCount: jobs.failed
        + errors.length
        + (unknownTasksReported ? 0 : tasks.unknown)
        + (unknownJobsReported ? 0 : jobs.unknown),
    };

    instance.recentActivity = asArray(raw.recent_activity)
      .map((item) => normalizeActivity(item, instance, "activity"));
    instance.recentTasks = asArray(raw.recent_tasks)
      .map((item) => normalizeActivity(item, instance, "task"));
    instance.timeline = [...instance.recentActivity, ...instance.recentTasks]
      .sort(compareActivity)
      .filter(deduplicateActivity());
    instance.lastActivity = instance.timeline.find((item) => item.time)?.time || null;
    return instance;
  }

  function normalizeSnapshot(payloadValue) {
    const payload = asObject(payloadValue);
    if (payload.version !== API_VERSION) {
      throw new Error(`Snapshot response requires API version ${API_VERSION}.`);
    }
    if (!Array.isArray(payload.instances)) {
      throw new Error("Snapshot response is missing its instances list.");
    }
    const generatedAtText = stringValue(payload.generated_at);
    const generatedAt = safeDate(generatedAtText) || new Date();
    const instances = payload.instances.map((item) => normalizeInstance(item, generatedAt));
    const computed = computeSummary(instances);
    const provided = asObject(payload.summary);
    const sourceErrors = asArray(payload.source_errors)
      .map(errorMessage)
      .filter(Boolean)
      .filter((item, index, all) => all.indexOf(item) === index);

    const summary = {
      ...computed,
      running: asNumber(provided.running, computed.running),
      providerIssues: asNumber(provided.provider_issues, computed.providerIssues),
      attention: asNumber(provided.attention, computed.attention),
      sourceErrors: sourceErrors.length,
    };
    summary.errors = Math.max(
      computed.errors + summary.sourceErrors,
      asNumber(provided.errors),
    );
    const activityShell = { id: "fleet", team: "Fleet" };
    const globalActivity = asArray(payload.recent_activity)
      .map((item) => normalizeActivity(item, activityShell, "activity"));
    const activity = [...globalActivity, ...instances.flatMap((item) => item.timeline)]
      .sort(compareActivity)
      .filter(deduplicateActivity())
      .slice(0, ACTIVITY_LIMIT);

    return { generatedAt, generatedAtText, summary, instances, activity, sourceErrors };
  }

  function computeSummary(instances) {
    const summary = {
      total: instances.length,
      running: 0,
      providerIssues: 0,
      attention: 0,
      tasks: { total: 0, open: 0, closed: 0, unknown: 0 },
      jobs: { total: 0, active: 0, done: 0, failed: 0, unknown: 0 },
      agents: { total: 0, active: 0 },
      errors: 0,
    };
    for (const instance of instances) {
      if (instance.container === "running") summary.running += 1;
      if (instance.health.state.startsWith("provider-")) summary.providerIssues = 1;
      if (instance.displayState === "attention") summary.attention += 1;
      summary.tasks.total += instance.tasks.total;
      summary.tasks.open += instance.tasks.open;
      summary.tasks.closed += instance.tasks.closed;
      summary.tasks.unknown += instance.tasks.unknown;
      summary.jobs.total += instance.jobs.total;
      summary.jobs.active += instance.jobs.pending + instance.jobs.claimed + instance.jobs.running;
      summary.jobs.done += instance.jobs.done;
      summary.jobs.failed += instance.jobs.failed;
      summary.jobs.unknown += instance.jobs.unknown;
      summary.agents.total += instance.agents.total;
      summary.agents.active += instance.agents.active;
      summary.errors += instance.errors.length;
    }
    return summary;
  }

  function validPort(value) {
    const port = Number(value);
    return Number.isInteger(port) && port >= 1 && port <= 65_535 ? port : 0;
  }

  function agentwsUrlForCurrentHost(portValue) {
    const port = validPort(portValue);
    if (!port) return "";
    try {
      const url = new URL("/", window.location.href);
      if (!["http:", "https:"].includes(url.protocol)) return "";
      url.port = String(port);
      return url.href;
    } catch (_error) {
      return "";
    }
  }

  function compareActivity(a, b) {
    const aTime = a.time ? a.time.getTime() : 0;
    const bTime = b.time ? b.time.getTime() : 0;
    return bTime - aTime;
  }

  function deduplicateActivity() {
    const seen = new Set();
    return (item) => {
      const key = `${item.instanceId}\u0000${item.message}\u0000${item.timestamp}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    };
  }

  function formatCount(value) {
    return numberFormatter.format(asNumber(value));
  }

  function plural(value, singular, pluralForm = `${singular}s`) {
    return `${formatCount(value)} ${value === 1 ? singular : pluralForm}`;
  }

  function relativeTime(value) {
    const date = safeDate(value);
    if (!date) return "Time unavailable";
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    const absolute = Math.abs(seconds);
    if (absolute < 8) return "just now";

    const ranges = [
      [60, "second"],
      [60, "minute"],
      [24, "hour"],
      [7, "day"],
      [4.345, "week"],
      [12, "month"],
      [Number.POSITIVE_INFINITY, "year"],
    ];
    let amount = seconds;
    for (const [boundary, unit] of ranges) {
      if (Math.abs(amount) < boundary) {
        const rounded = Math.round(amount);
        if (typeof Intl.RelativeTimeFormat === "function") {
          return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(rounded, unit);
        }
        return rounded < 0 ? `${Math.abs(rounded)} ${unit}${Math.abs(rounded) === 1 ? "" : "s"} ago` : `in ${rounded} ${unit}${rounded === 1 ? "" : "s"}`;
      }
      amount /= boundary;
    }
    return dateFormatter.format(date);
  }

  function stateLabel(instance) {
    return instance.container || "unknown";
  }

  function avatarColor(value) {
    const palette = [
      { fill: "#D8402E", ink: "#EFE7D6" },
      { fill: "#2A5CAA", ink: "#EFE7D6" },
      { fill: "#EFB02C", ink: "#201A12" },
      { fill: "#3E7C5A", ink: "#EFE7D6" },
    ];
    let hash = 0;
    for (const character of value) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
    return palette[Math.abs(hash) % palette.length];
  }

  function setConnection(status, label) {
    elements.connection.dataset.state = status;
    elements.connectionLabel.textContent = label;
  }

  function showNotice(message, kind = "error") {
    elements.noticeMessage.textContent = message;
    elements.notice.dataset.kind = kind;
    elements.notice.hidden = false;
  }

  function hideNotice() {
    elements.notice.hidden = true;
  }

  function renderSummary(summary) {
    elements.statInstances.textContent = formatCount(summary.total);
    elements.statRunning.textContent = `${formatCount(summary.running)} running · ${formatCount(summary.total - summary.running)} not running`;
    elements.statTasks.textContent = formatCount(summary.tasks.open);
    const taskSummary = [
      plural(summary.tasks.total, "task"),
      `${formatCount(summary.tasks.closed)} closed`,
    ];
    if (summary.tasks.unknown) taskSummary.push(`${formatCount(summary.tasks.unknown)} unknown`);
    elements.statTasksDetail.textContent = taskSummary.join(" · ");
    elements.statAgents.textContent = formatCount(summary.agents.active);
    elements.statAgentsDetail.textContent = `${plural(summary.agents.total, "agent")} configured`;
    elements.statAttention.textContent = formatCount(summary.attention);
    elements.statAttentionDetail.textContent = `${plural(summary.jobs.failed, "failed job")} · ${plural(summary.providerIssues, "provider issue")} · ${plural(summary.errors, "data error")}`;
    elements.attentionStat.classList.toggle("has-attention", summary.attention > 0);
  }

  function modeBadges(instance) {
    return [
      {
        label: instance.mode.offline ? "network: offline" : "network: online",
        className: instance.mode.offline ? "mode-badge--offline" : "mode-badge--online",
      },
      {
        label: instance.mode.teamWrite ? "team: writable" : "team: read-only",
        className: instance.mode.teamWrite ? "mode-badge--write" : "mode-badge--readonly",
      },
    ];
  }

  function appendModeBadges(container, instance) {
    for (const mode of modeBadges(instance)) {
      const badge = document.createElement("span");
      badge.className = `mode-badge ${mode.className}`;
      badge.textContent = mode.label;
      container.append(badge);
    }
  }

  function renderCardActivity(container, timeline) {
    const items = timeline.slice(0, CARD_ACTIVITY_LIMIT);
    if (!items.length) return;
    container.hidden = false;
    const list = container.querySelector("ol");
    for (const activity of items) {
      const row = document.createElement("li");
      const message = document.createElement("span");
      const time = document.createElement("time");
      message.textContent = activity.actor ? `${activity.actor}: ${activity.message}` : activity.message;
      message.title = message.textContent;
      time.textContent = activity.time ? relativeTime(activity.time) : activity.status;
      if (activity.time) {
        time.dateTime = activity.time.toISOString();
        time.title = dateFormatter.format(activity.time);
      }
      row.append(message, time);
      list.append(row);
    }
  }

  function renderErrors(container, errors) {
    if (!errors.length) return;
    container.hidden = false;
    container.querySelector("strong").textContent = plural(errors.length, "issue");
    const list = container.querySelector("ul");
    for (const message of errors.slice(0, 2)) {
      const item = document.createElement("li");
      item.textContent = message;
      list.append(item);
    }
    if (errors.length > 2) {
      const item = document.createElement("li");
      item.textContent = `and ${errors.length - 2} more`;
      list.append(item);
    }
  }

  function renderInstanceCard(instance) {
    const fragment = elements.instanceTemplate.content.cloneNode(true);
    const card = fragment.querySelector(".instance-card");
    const avatar = fragment.querySelector(".instance-avatar");
    const name = fragment.querySelector(".instance-name");
    const identifier = fragment.querySelector(".instance-id");
    const pill = fragment.querySelector(".state-pill");
    const workspaceLink = fragment.querySelector(".agentws-link");

    card.dataset.state = instance.displayState;
    card.dataset.instanceId = instance.id;
    avatar.textContent = instance.team.slice(0, 2);
    const avatarHue = avatarColor(instance.team);
    avatar.style.setProperty("--avatar-color", avatarHue.fill);
    avatar.style.setProperty("--avatar-ink", avatarHue.ink);
    name.textContent = instance.team;
    name.title = instance.teamReference;
    identifier.textContent = instance.id;
    identifier.title = instance.id;
    pill.dataset.state = instance.state;
    pill.querySelector("b").textContent = stateLabel(instance);

    const workspaceIsAvailable = Boolean(instance.agentwsUrl) && instance.operational && !instance.mode.offline;
    workspaceLink.hidden = !workspaceIsAvailable;
    if (workspaceIsAvailable) {
      workspaceLink.href = instance.agentwsUrl;
      workspaceLink.setAttribute("aria-label", `Open ${instance.team} in AgentWS`);
    }

    appendModeBadges(fragment.querySelector(".mode-list"), instance);

    fragment.querySelector(".desired-state").textContent = instance.desired;
    fragment.querySelector(".container-state").textContent = instance.container;
    fragment.querySelector(".readiness-state").textContent = instance.readiness;
    const providerHealth = fragment.querySelector(".provider-health");
    providerHealth.dataset.state = instance.health.state;
    providerHealth.textContent = instance.health.state === "inactive"
      ? "—"
      : [instance.health.state, instance.health.reason].filter(Boolean).join(" · ");
    providerHealth.title = instance.health.reason;

    const project = fragment.querySelector(".project-path");
    project.textContent = instance.project;
    project.title = [instance.projectDescription, instance.projectReference]
      .filter(Boolean)
      .join("\n");
    const describeLocations = (locations, singular) => {
      if (!locations.length) return "None";
      if (locations.length === 1) {
        return `${locations[0].name} → ${locations[0].containerPath}`;
      }
      return plural(locations.length, singular);
    };
    const locationTitle = (locations) => locations
      .map((mount) => `${mount.name}: ${mount.path} → ${mount.containerPath}`)
      .join("\n");
    const workspaces = fragment.querySelector(".workspace-paths");
    workspaces.textContent = describeLocations(instance.workspaces, "workspace");
    workspaces.title = locationTitle(instance.workspaces);
    const readOnlyMounts = fragment.querySelector(".readonly-paths");
    readOnlyMounts.textContent = describeLocations(instance.readOnlyMounts, "mount");
    readOnlyMounts.title = locationTitle(instance.readOnlyMounts);
    const generation = fragment.querySelector(".generation");
    generation.textContent = instance.generation;
    generation.title = instance.generation;

    fragment.querySelector(".task-count").textContent = formatCount(instance.tasks.open);
    const taskDetails = [
      `${formatCount(instance.tasks.total)} total`,
      `${formatCount(instance.tasks.closed)} closed`,
    ];
    if (instance.tasks.unknown) taskDetails.push(`${formatCount(instance.tasks.unknown)} unknown`);
    fragment.querySelector(".task-detail").textContent = taskDetails.join(" · ");
    const activeJobs = instance.jobs.pending + instance.jobs.claimed + instance.jobs.running;
    fragment.querySelector(".job-count").textContent = formatCount(activeJobs);
    const jobDetails = [
      `${formatCount(instance.jobs.done)} done`,
      `${formatCount(instance.jobs.failed)} failed`,
    ];
    if (instance.jobs.unknown) jobDetails.push(`${formatCount(instance.jobs.unknown)} unknown`);
    fragment.querySelector(".job-detail").textContent = jobDetails.join(" · ");
    fragment.querySelector(".agent-count").textContent = formatCount(instance.agents.active);
    fragment.querySelector(".agent-detail").textContent = `${formatCount(instance.agents.total)} configured`;

    renderErrors(fragment.querySelector(".instance-errors"), instance.errors);
    renderCardActivity(fragment.querySelector(".card-activity"), instance.timeline);

    const lastActive = fragment.querySelector(".last-active");
    lastActive.textContent = instance.lastActivity
      ? `Activity ${relativeTime(instance.lastActivity)}`
      : "No recent activity";
    if (instance.lastActivity) lastActive.title = dateFormatter.format(instance.lastActivity);
    fragment.querySelector(".ui-unavailable").hidden = workspaceIsAvailable;
    return fragment;
  }

  function filteredInstances() {
    if (!state.snapshot) return [];
    const query = elements.searchInput.value.trim().toLocaleLowerCase();
    const status = elements.statusFilter.value;
    const mode = elements.modeFilter.value;

    const instances = state.snapshot.instances.filter((instance) => {
      const searchText = [
        instance.team,
        instance.teamReference,
        instance.project,
        instance.projectDescription,
        instance.projectReference,
        ...instance.workspaces.flatMap((mount) => [mount.name, mount.path, mount.containerPath]),
        ...instance.readOnlyMounts.flatMap((mount) => [mount.name, mount.path, mount.containerPath]),
        instance.id,
        instance.generation,
        instance.desired,
        instance.container,
        instance.readiness,
        instance.rawState,
        instance.health.state,
        instance.health.reason,
        ...instance.errors,
      ].join(" ").toLocaleLowerCase();
      const queryMatches = !query || searchText.includes(query);
      const statusMatches = status === "all"
        || (status === "attention" ? instance.displayState === "attention" : instance.state === status);
      const modeMatches = mode === "all"
        || (mode === "online" && !instance.mode.offline)
        || (mode === "offline" && instance.mode.offline)
        || (mode === "team-write" && instance.mode.teamWrite);
      return queryMatches && statusMatches && modeMatches;
    });

    const sort = elements.sortSelect.value;
    instances.sort((a, b) => {
      if (sort === "name") return a.team.localeCompare(b.team, undefined, { sensitivity: "base" });
      if (sort === "failures") return b.failureCount - a.failureCount || a.team.localeCompare(b.team);
      const aTime = a.lastActivity ? a.lastActivity.getTime() : 0;
      const bTime = b.lastActivity ? b.lastActivity.getTime() : 0;
      return bTime - aTime || a.team.localeCompare(b.team);
    });
    return instances;
  }

  function filtersAreActive() {
    return Boolean(elements.searchInput.value.trim())
      || elements.statusFilter.value !== "all"
      || elements.modeFilter.value !== "all";
  }

  function renderInstances() {
    if (!state.snapshot) return;
    const all = state.snapshot.instances;
    const visible = filteredInstances();
    elements.instanceGrid.replaceChildren();
    elements.loadingState.hidden = true;

    if (!all.length) {
      elements.instanceGrid.hidden = true;
      elements.emptyState.hidden = false;
      elements.emptyTitle.textContent = "No instances yet";
      elements.emptyMessage.textContent = "Start a team with cyclo run and it will appear here.";
      elements.clearFilters.hidden = true;
      elements.resultCount.textContent = "0 instances";
      return;
    }

    if (!visible.length) {
      elements.instanceGrid.hidden = true;
      elements.emptyState.hidden = false;
      elements.emptyTitle.textContent = "Nothing matches";
      elements.emptyMessage.textContent = "Try a different team name, state, or execution mode.";
      elements.clearFilters.hidden = false;
      elements.resultCount.textContent = `0 of ${formatCount(all.length)} instances`;
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const instance of visible) fragment.append(renderInstanceCard(instance));
    elements.instanceGrid.append(fragment);
    elements.instanceGrid.hidden = false;
    elements.emptyState.hidden = true;
    elements.resultCount.textContent = visible.length === all.length
      ? plural(all.length, "instance")
      : `${formatCount(visible.length)} of ${formatCount(all.length)} instances`;
  }

  function renderActivity(activity) {
    elements.activityStream.replaceChildren();
    if (!activity.length) {
      elements.activityPanel.hidden = true;
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of activity) {
      const row = document.createElement("li");
      const dot = document.createElement("span");
      const team = document.createElement("span");
      const message = document.createElement("span");
      const time = document.createElement("time");
      dot.className = "activity-stream__dot";
      dot.setAttribute("aria-hidden", "true");
      team.className = "activity-stream__team";
      team.textContent = item.team;
      message.className = "activity-stream__message";
      message.textContent = item.actor ? `${item.actor}: ${item.message}` : item.message;
      message.title = message.textContent;
      time.textContent = item.time ? relativeTime(item.time) : item.status;
      if (item.time) {
        time.dateTime = item.time.toISOString();
        time.title = dateFormatter.format(item.time);
      }
      row.append(dot, team, message, time);
      fragment.append(row);
    }
    elements.activityStream.append(fragment);
    elements.activityPanel.hidden = false;
  }

  function renderSnapshot() {
    if (!state.snapshot) {
      elements.loadingState.hidden = state.loading === false;
      if (!state.loading && state.error) {
        elements.instanceGrid.hidden = true;
        elements.emptyState.hidden = false;
        elements.emptyTitle.textContent = "Dashboard unavailable";
        elements.emptyMessage.textContent = "Cyclo could not read the local snapshot. Check that the dashboard process is still running, then refresh.";
        elements.clearFilters.hidden = true;
        elements.resultCount.textContent = "Snapshot unavailable";
      }
      return;
    }
    renderSummary(state.snapshot.summary);
    renderInstances();
    renderActivity(state.snapshot.activity);
    renderClock();
  }

  function renderClock() {
    if (state.snapshot) {
      elements.snapshotTime.textContent = relativeTime(state.snapshot.generatedAt);
      elements.snapshotTime.title = dateFormatter.format(state.snapshot.generatedAt);
    }
    if (!elements.autoRefresh.checked) {
      elements.refreshCountdown.textContent = "Live refresh paused";
      return;
    }
    if (state.loading) {
      elements.refreshCountdown.textContent = "Refreshing snapshot…";
      return;
    }
    if (!state.nextRefreshAt) {
      elements.refreshCountdown.textContent = document.hidden ? "Refresh paused in background" : "Auto-refresh enabled";
      return;
    }
    const seconds = Math.max(0, Math.ceil((state.nextRefreshAt - Date.now()) / 1000));
    elements.refreshCountdown.textContent = `Next refresh in ${seconds}s`;
  }

  function clearRefreshTimer() {
    if (state.refreshTimer) window.clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
    state.nextRefreshAt = null;
  }

  function scheduleRefresh() {
    clearRefreshTimer();
    if (!elements.autoRefresh.checked || document.hidden) {
      renderClock();
      return;
    }
    state.nextRefreshAt = Date.now() + REFRESH_INTERVAL_MS;
    state.refreshTimer = window.setTimeout(() => loadSnapshot(), REFRESH_INTERVAL_MS);
    renderClock();
  }

  async function loadSnapshot({ force = false } = {}) {
    if (state.activeRequest) {
      if (!force) return;
      state.activeRequest.abort();
    }
    clearRefreshTimer();
    const controller = new AbortController();
    state.activeRequest = controller;
    state.loading = true;
    elements.refreshButton.disabled = true;
    elements.refreshButton.classList.add("is-loading");
    setConnection("loading", state.snapshot ? "Refreshing" : "Connecting");
    renderClock();

    try {
      const response = await fetch(API_URL, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Snapshot request failed (${response.status})`);
      const payload = await response.json();
      state.snapshot = normalizeSnapshot(payload);
      state.fetchedAt = new Date();
      state.error = null;
      if (state.snapshot.sourceErrors.length) {
        const details = state.snapshot.sourceErrors.slice(0, 2).join(" · ");
        const remainder = state.snapshot.sourceErrors.length > 2
          ? ` · ${state.snapshot.sourceErrors.length - 2} more`
          : "";
        showNotice(`Some dashboard data is incomplete: ${details}${remainder}`, "warning");
      } else {
        hideNotice();
      }
      setConnection("online", "Live");
    } catch (error) {
      if (error.name === "AbortError") return;
      state.error = error instanceof Error ? error.message : "Could not load the Cyclo snapshot.";
      setConnection("error", "Disconnected");
      if (state.snapshot) {
        showNotice(`Live refresh failed. Showing the last good snapshot. ${state.error}`);
      } else {
        showNotice(state.error);
      }
    } finally {
      if (state.activeRequest !== controller) return;
      state.activeRequest = null;
      state.loading = false;
      elements.refreshButton.disabled = false;
      elements.refreshButton.classList.remove("is-loading");
      renderSnapshot();
      scheduleRefresh();
    }
  }

  function clearFilters() {
    elements.searchInput.value = "";
    elements.statusFilter.value = "all";
    elements.modeFilter.value = "all";
    renderInstances();
    elements.searchInput.focus();
  }

  function restoreAutoRefreshPreference() {
    try {
      const preference = window.localStorage.getItem("cyclo.dashboard.live");
      if (preference !== null) elements.autoRefresh.checked = preference === "true";
    } catch (_error) {
      // Storage can be disabled; live refresh still works for this page load.
    }
  }

  function saveAutoRefreshPreference() {
    try {
      window.localStorage.setItem("cyclo.dashboard.live", String(elements.autoRefresh.checked));
    } catch (_error) {
      // Ignore storage failures; the control remains functional in memory.
    }
  }

  function bindEvents() {
    elements.refreshButton.addEventListener("click", () => loadSnapshot({ force: true }));
    elements.noticeDismiss.addEventListener("click", hideNotice);
    elements.clearFilters.addEventListener("click", clearFilters);
    elements.searchInput.addEventListener("input", renderInstances);
    elements.statusFilter.addEventListener("change", renderInstances);
    elements.modeFilter.addEventListener("change", renderInstances);
    elements.sortSelect.addEventListener("change", renderInstances);
    elements.autoRefresh.addEventListener("change", () => {
      saveAutoRefreshPreference();
      scheduleRefresh();
    });
    document.addEventListener("keydown", (event) => {
      const target = event.target;
      const isTyping = target instanceof HTMLInputElement
        || target instanceof HTMLTextAreaElement
        || target instanceof HTMLSelectElement
        || target?.isContentEditable;
      if (event.key === "/" && !isTyping && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        elements.searchInput.focus();
      }
      if (event.key === "Escape" && document.activeElement === elements.searchInput && elements.searchInput.value) {
        elements.searchInput.value = "";
        renderInstances();
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearRefreshTimer();
        renderClock();
        return;
      }
      const stale = !state.fetchedAt || Date.now() - state.fetchedAt.getTime() >= REFRESH_INTERVAL_MS;
      if (elements.autoRefresh.checked && stale) loadSnapshot();
      else scheduleRefresh();
    });
  }

  function init() {
    restoreAutoRefreshPreference();
    bindEvents();
    window.setInterval(() => {
      renderClock();
    }, 1_000);
    loadSnapshot();
  }

  init();
})();

const dashboardState = {
  payload: null,
  dashboard: null,
  signature: "",
  scale: null,
  playheadEl: null,
  follow: false,
  playheadSync: null,
  sets: [],
  activeSet: null,
  selectedSet: null,
};

const els = {
  nowTitle: document.querySelector("#now-title"),
  nowMeta: document.querySelector("#now-meta"),
  transportStatus: document.querySelector("#transport-status"),
  playheadTime: document.querySelector("#playhead-time"),
  windowTime: document.querySelector("#window-time"),
  updatedTime: document.querySelector("#updated-time"),
  currentTitle: document.querySelector("#current-title"),
  currentState: document.querySelector("#current-state"),
  currentDetail: document.querySelector("#current-detail"),
  sessionProgress: document.querySelector("#session-progress"),
  nextList: document.querySelector("#next-list"),
  commentaryList: document.querySelector("#commentary-list"),
  healthList: document.querySelector("#health-list"),
  automationList: document.querySelector("#automation-list"),
  sessionSummary: document.querySelector("#session-summary"),
  archiveTitle: document.querySelector("#archive-title"),
  archiveStatus: document.querySelector("#archive-status"),
  archiveList: document.querySelector("#archive-list"),
  viewActiveSet: document.querySelector("#view-active-set"),
  newSet: document.querySelector("#new-set"),
  saveLoadedSet: document.querySelector("#save-loaded-set"),
  timelineTitle: document.querySelector("#timeline-title"),
  timeAxis: document.querySelector("#time-axis"),
  timelineScroll: document.querySelector("#timeline-scroll"),
  timeline: document.querySelector("#timeline"),
  followPlayhead: document.querySelector("#follow-playhead"),
  mixerChannels: document.querySelector("#mixer-channels"),
  crossfaderStrip: document.querySelector("#crossfader-strip"),
};

const MIN_STAGE_WIDTH = 1600;
const LANE_LABEL_WIDTH = 104;
const MIXER_DECK_ORDER = ["deck-1", "deck-5", "deck-2", "deck-3", "deck-4"];
const DECK_LABELS = { "deck-5": "MIC" };
const KNOB_DEFS = [
  { key: "trim", label: "trim", min: -12, max: 12, neutral: 0, unit: "dB" },
  { key: "hi", label: "hi", min: -12, max: 12, neutral: 0, unit: "dB" },
  { key: "mid", label: "mid", min: -12, max: 12, neutral: 0, unit: "dB" },
  { key: "low", label: "low", min: -12, max: 12, neutral: 0, unit: "dB" },
  { key: "filter", label: "filter", min: -1, max: 1, neutral: 0, unit: "" },
];
const KNOB_MIN_DEG = -135;
const KNOB_MAX_DEG = 135;

function fmtMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "--:--";
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function shortPath(path) {
  if (!path) return "";
  const parts = path.split("/");
  return parts.slice(Math.max(0, parts.length - 3)).join(" / ");
}

function generatedAtMs() {
  const value = Date.parse(dashboardState.payload?.generated_at || "");
  return Number.isNaN(value) ? Date.now() : value;
}

function livePlayheadMs() {
  const sync = dashboardState.playheadSync;
  if (sync) {
    if (!["playing", "window-active"].includes(sync.status)) return sync.baseMs;
    return Math.min(sync.durationMs || sync.baseMs, sync.baseMs + Math.max(0, performance.now() - sync.clientMs));
  }
  const transport = dashboardState.dashboard?.transport || {};
  const base = transport.playhead_ms;
  if (base === null || base === undefined) return null;
  if (!["playing", "window-active"].includes(transport.status)) return base;
  const duration = transport.duration_ms || base;
  return Math.min(duration, base + Math.max(0, Date.now() - generatedAtMs()));
}

function syncPlayhead(transport) {
  const base = transport?.playhead_ms;
  if (base === null || base === undefined) {
    dashboardState.playheadSync = null;
    return;
  }
  const current = livePlayheadMs();
  const drift = current === null ? Infinity : Math.abs(current - base);
  const statusChanged = dashboardState.playheadSync?.status !== transport.status;
  if (!dashboardState.playheadSync || drift > 1500 || statusChanged) {
    dashboardState.playheadSync = {
      baseMs: base,
      clientMs: performance.now(),
      durationMs: transport.duration_ms || base,
      status: transport.status,
    };
    return;
  }
  dashboardState.playheadSync.durationMs = transport.duration_ms || base;
  dashboardState.playheadSync.status = transport.status;
}

function eventSignature(dashboard) {
  return JSON.stringify(
    (dashboard?.events || []).map((event) => [
      event.id,
      event.kind,
      event.lane,
      event.status,
      event.start_ms,
      event.end_ms,
      event.display_title,
      event.display_meta,
      event.style_flags,
    ])
  );
}

function statusLabel(value) {
  return String(value || "unknown").replace("-", " ");
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function lerp(a, b, pct) {
  return a + (b - a) * pct;
}

function numericValue(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function automationValue(points, atMs, fallback = null) {
  const valid = (points || [])
    .map((point) => ({ at: numericValue(point.at_ms, null), value: numericValue(point.value, null) }))
    .filter((point) => point.at !== null && point.value !== null)
    .sort((a, b) => a.at - b.at);
  if (!valid.length || atMs === null || atMs === undefined) return fallback;
  if (atMs <= valid[0].at) return valid[0].value;
  for (let index = 1; index < valid.length; index += 1) {
    const left = valid[index - 1];
    const right = valid[index];
    if (atMs <= right.at) {
      const span = Math.max(1, right.at - left.at);
      return lerp(left.value, right.value, clamp((atMs - left.at) / span, 0, 1));
    }
  }
  return valid[valid.length - 1].value;
}

function automationFor(targetId, param, atMs, fallback = null) {
  const events = dashboardState.dashboard?.events || [];
  const automation = events
    .filter((event) => event.kind === "automation" && event.param === param && (event.target === targetId || event.owner === targetId))
    .sort((a, b) => numericValue(a.start_ms, 0) - numericValue(b.start_ms, 0));
  let value = fallback;
  for (const event of automation) {
    const start = numericValue(event.start_ms, null);
    const end = numericValue(event.end_ms, null);
    if (start === null || end === null || atMs === null || atMs === undefined) continue;
    if (atMs >= start && atMs <= end) value = automationValue(event.points, atMs, value);
  }
  return value;
}

function pickDeckEvent(deckId, playhead) {
  const events = (dashboardState.dashboard?.events || []).filter((event) => event.lane === deckId && ["song", "vocal"].includes(event.kind));
  return (
    events.find((event) => playhead !== null && event.start_ms <= playhead && playhead < event.end_ms) ||
    events.find((event) => playhead !== null && event.start_ms >= playhead) ||
    events[events.length - 1] ||
    null
  );
}

function filterState(event, playhead) {
  if (!event) return 0;
  const lowpass = automationFor(event.id, "lowpass_hz", playhead, null);
  const highpass = automationFor(event.id, "highpass_hz", playhead, null);
  if (Number.isFinite(highpass) && highpass > 30) return clamp(highpass / 2500, 0, 1);
  if (Number.isFinite(lowpass) && lowpass > 0 && lowpass < 18_000) return -clamp((18_000 - lowpass) / 18_000, 0, 1);
  return 0;
}

function channelState(deckId) {
  const playhead = livePlayheadMs();
  const event = pickDeckEvent(deckId, playhead);
  const isMic = deckId === "deck-5";
  const trim = isMic ? 0 : numericValue(event?.trim_db, 0);
  const gain = isMic
    ? (numericValue(event?.volume, 1) - 1) * 12
    : automationFor(event?.id, "gain_db", playhead, numericValue(event?.gain_db, 0));
  const eqLow = automationFor(event?.id, "eq_low_db", playhead, 0);
  const eqMid = automationFor(event?.id, "eq_mid_db", playhead, 0);
  const eqHigh = automationFor(event?.id, "eq_high_db", playhead, 0);
  const filter = filterState(event, playhead);
  const level = clamp((gain + trim + 30) / 42, 0, 1);
  return {
    id: deckId,
    label: DECK_LABELS[deckId] || deckId.replace("deck-", ""),
    route: dashboardState.dashboard?.session?.fader_assignments?.[deckId] || (isMic ? "THRU" : ""),
    title: event?.display_title || "empty",
    meta: event?.display_meta || "",
    active: event?.status === "current",
    values: {
      trim,
      hi: eqHigh,
      mid: eqMid,
      low: eqLow,
      filter,
      fader: gain,
      level,
    },
  };
}

function knobDeg(def, value) {
  const pct = clamp((numericValue(value, def.neutral) - def.min) / (def.max - def.min), 0, 1);
  return lerp(KNOB_MIN_DEG, KNOB_MAX_DEG, pct);
}

function knobValueText(def, value) {
  if (def.key === "filter") {
    if (Math.abs(value) < 0.02) return "center";
    return value < 0 ? `lp ${Math.round(Math.abs(value) * 100)}%` : `hp ${Math.round(value * 100)}%`;
  }
  return `${numericValue(value, 0).toFixed(1)}${def.unit}`;
}

function renderKnob(def, value) {
  const wrap = document.createElement("div");
  wrap.className = "knob-control";
  const knob = document.createElement("div");
  knob.className = "knob";
  knob.style.setProperty("--knob-deg", `${knobDeg(def, value)}deg`);
  knob.title = `${def.label}: ${knobValueText(def, value)}`;
  const label = document.createElement("span");
  label.textContent = def.label;
  wrap.append(knob, label);
  return wrap;
}

function sliderValueFromDb(value) {
  return clamp(((numericValue(value, 0) + 36) / 42) * 100, 0, 100);
}

function renderMixer() {
  if (!els.mixerChannels || !els.crossfaderStrip) return;
  const channels = MIXER_DECK_ORDER.map(channelState);
  els.mixerChannels.replaceChildren();
  for (const channel of channels) {
    const strip = document.createElement("article");
    strip.className = `mixer-channel ${channel.active ? "active" : ""} ${channel.id === "deck-5" ? "mic" : ""}`;
    const head = document.createElement("div");
    head.className = "mixer-channel-head";
    head.innerHTML = `<strong>${channel.label}</strong><span>${channel.route || "deck"}</span>`;
    const title = document.createElement("p");
    title.className = "mixer-channel-title";
    title.textContent = channel.title;
    const knobs = document.createElement("div");
    knobs.className = "knob-grid";
    for (const def of KNOB_DEFS) knobs.append(renderKnob(def, channel.values[def.key]));
    const fader = document.createElement("div");
    fader.className = "channel-fader";
    const level = Math.round(channel.values.level * 100);
    fader.innerHTML = `
      <label>
        <span>level</span>
        <input type="range" min="0" max="100" value="${level}" disabled aria-label="${channel.label} level" />
      </label>
      <label>
        <span>fader</span>
        <input type="range" min="0" max="100" value="${Math.round(sliderValueFromDb(channel.values.fader))}" disabled aria-label="${channel.label} fader" />
      </label>
    `;
    strip.append(head, title, knobs, fader);
    els.mixerChannels.append(strip);
  }
  const crossfader = (dashboardState.dashboard?.events || []).find((event) => event.kind === "automation" && event.target === "crossfader" && event.param === "position");
  const position = automationValue(crossfader?.points, livePlayheadMs(), 0);
  els.crossfaderStrip.innerHTML = `
    <span>A</span>
    <input type="range" min="-100" max="100" value="${Math.round(clamp(position, -1, 1) * 100)}" disabled aria-label="crossfader position" />
    <span>B</span>
  `;
}

function setBadgeState(el, status) {
  el.className = `badge ${String(status || "unknown").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
  el.textContent = statusLabel(status);
}

function renderTopline() {
  const dashboard = dashboardState.dashboard;
  const now = dashboard?.now;
  const transport = dashboard?.transport || {};
  const playhead = livePlayheadMs();
  const viewedSet = dashboard?.viewed_set;
  const activeSet = dashboard?.active_set;

  els.nowTitle.textContent = viewedSet?.title || now?.display_title || activeSet?.title || "nothing active";
  els.nowMeta.textContent = viewedSet
    ? `archived view | ${viewedSet.slug}`
    : now
      ? now.display_meta || shortPath(now.path)
      : dashboard?.session_path || "waiting for runner state";
  els.transportStatus.textContent = viewedSet ? "archived" : statusLabel(transport.status);
  els.playheadTime.textContent = fmtMs(playhead);
  els.windowTime.textContent = transport.window?.start_ms !== undefined
    ? `${fmtMs(transport.window.start_ms)} - ${fmtMs(transport.window.end_ms)}`
    : "--:--";
  els.updatedTime.textContent = transport.updated_at || dashboardState.payload?.generated_at || "--";

  els.currentTitle.textContent = now?.display_title || "nothing active";
  els.currentDetail.textContent = now ? `${fmtMs(now.start_ms)} - ${fmtMs(now.end_ms)} | ${shortPath(now.path)}` : "no current clip";
  setBadgeState(els.currentState, transport.status || "idle");
  const pct = transport.duration_ms && playhead !== null ? Math.max(0, Math.min(100, (playhead / transport.duration_ms) * 100)) : 0;
  els.sessionProgress.style.width = `${pct}%`;
}

function listItem(event) {
  const item = document.createElement("div");
  item.className = `mini-event ${event.kind || "event"} ${event.status || ""}`;
  const title = document.createElement("strong");
  title.textContent = event.display_title || "untitled";
  const meta = document.createElement("span");
  meta.textContent = `${fmtMs(event.start_ms)} | ${event.display_meta || shortPath(event.path)}`;
  item.append(title, meta);
  return item;
}

function renderList(container, events, emptyText, limit = 6) {
  container.replaceChildren();
  if (!events || !events.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = emptyText;
    container.append(empty);
    return;
  }
  for (const event of events.slice(0, limit)) container.append(listItem(event));
}

function renderHealth() {
  const dashboard = dashboardState.dashboard || {};
  const health = dashboard.health || {};
  els.healthList.replaceChildren();
  const rows = [
    ["runner", health.runner_state || "unknown"],
    ["current clips", String((health.current_clips || []).length)],
    ["receiver telemetry", (health.receivers || []).length ? `${health.receivers.length} receivers` : "not in state"],
  ];
  for (const [key, value] of rows) {
    const row = document.createElement("div");
    row.className = "health-row";
    row.innerHTML = `<span>${key}</span><strong>${value}</strong>`;
    els.healthList.append(row);
  }
}

function renderSummary() {
  const dashboard = dashboardState.dashboard || {};
  const session = dashboard.session || {};
  const counts = session.counts || {};
  const setInfo = dashboard.viewed_set || dashboard.active_set || {};
  const assignments = session.fader_assignments || {};
  const faderText = Object.keys(assignments).length
    ? Object.entries(assignments).map(([deck, side]) => `${deck}:${side}`).join(" ")
    : "default";
  const rows = [
    ["set", setInfo.title || setInfo.slug || "unassigned"],
    ["mode", session.timeline_mode || "native"],
    ["duration", fmtMs(session.duration_ms)],
    ["songs", counts.song || 0],
    ["fx clips", counts["effect-track"] || 0],
    ["effects", counts.effect || 0],
    ["slip", counts.slip || 0],
    ["vocal", counts.vocal || 0],
    ["automation", counts.automation || 0],
    ["fader", faderText],
    ["session", dashboard.session_path || ""],
  ];
  els.sessionSummary.replaceChildren();
  for (const [key, value] of rows) {
    const row = document.createElement("div");
    row.className = "summary-row";
    row.innerHTML = `<span>${key}</span><strong>${value}</strong>`;
    els.sessionSummary.append(row);
  }
}

function renderArchive() {
  const selected = dashboardState.selectedSet;
  const activeSlug = dashboardState.activeSet?.slug;
  els.archiveTitle.textContent = selected ? `viewing ${selected}` : dashboardState.activeSet?.title || "active set";
  els.archiveStatus.textContent = selected
    ? "archived session view, playback untouched"
    : activeSlug
      ? `loaded ${activeSlug}`
      : "no active set pointer";
  els.archiveList.replaceChildren();
  if (!dashboardState.sets.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "no archived sets yet";
    els.archiveList.append(empty);
    return;
  }
  for (const set of dashboardState.sets.slice(0, 12)) {
    const row = document.createElement("div");
    row.className = `archive-row ${set.slug === activeSlug ? "active" : ""} ${set.slug === selected ? "selected" : ""}`;
    const info = document.createElement("div");
    info.className = "archive-info";
    info.innerHTML = `<strong>${set.title || set.slug}</strong><span>${fmtMs(set.duration_ms || 0)} | ${set.slug}</span>`;
    const actions = document.createElement("div");
    actions.className = "archive-row-actions";
    actions.innerHTML = `
      <button type="button" data-action="view" data-slug="${set.slug}">view</button>
      <button type="button" data-action="activate" data-slug="${set.slug}">load</button>
      <button type="button" data-action="replay" data-slug="${set.slug}">play</button>
      <button type="button" data-action="render" data-slug="${set.slug}">render</button>
    `;
    row.append(info, actions);
    els.archiveList.append(row);
  }
}

function timelineScale(durationMs) {
  const duration = Math.max(60_000, durationMs || 60_000);
  const stageWidth = Math.max(MIN_STAGE_WIDTH, Math.ceil(duration / 1000) * 7);
  return { duration, stageWidth };
}

function renderAxis(scale) {
  els.timeAxis.replaceChildren();
  els.timeAxis.style.setProperty("--stage-width", `${scale.stageWidth}px`);
  const tickEvery = scale.duration > 3_600_000 ? 900_000 : scale.duration > 900_000 ? 300_000 : 60_000;
  for (let at = 0; at <= scale.duration; at += tickEvery) {
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.style.left = `${LANE_LABEL_WIDTH + (at / scale.duration) * scale.stageWidth}px`;
    tick.textContent = fmtMs(at);
    els.timeAxis.append(tick);
  }
  syncAxis();
}

function laneNumber(laneId) {
  const match = /^deck-(\d+)$/.exec(laneId || "");
  return match ? match[1] : "";
}

function fxLaneNumber(laneId) {
  const match = /^deck-(\d+)-fx$/.exec(laneId || "");
  return match ? match[1] : "";
}

function cssToken(value) {
  return String(value || "")
    .replace(/[^a-z0-9_-]+/gi, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
}

function renderTimeline() {
  const dashboard = dashboardState.dashboard;
  const scale = timelineScale(dashboard?.session?.duration_ms);
  dashboardState.scale = scale;
  els.timeline.replaceChildren();
  els.timeline.style.setProperty("--stage-width", `${scale.stageWidth}px`);
  renderAxis(scale);

  for (const lane of dashboard?.lanes || []) {
    const row = document.createElement("div");
    row.className = `lane-row ${lane.kind || ""}`;
    const label = document.createElement("div");
    label.className = "lane-label";
    const number = laneNumber(lane.id);
    const fxNumber = fxLaneNumber(lane.id);
    const utilityLabel = lane.id === "voice" ? "mic" : lane.id === "automation" ? "auto" : lane.id === "fader" ? "xfade" : lane.label || lane.id;
    if (lane.id === "deck-5") {
      label.innerHTML = `<strong>MIC</strong><span>vocal</span>`;
    } else if (number) {
      label.innerHTML = `<strong>${number}</strong><span>deck</span>`;
    } else if (fxNumber) {
      label.innerHTML = `<strong>${fxNumber}</strong><span>fx lane</span>`;
    } else {
      label.innerHTML = `<strong>${utilityLabel}</strong><span>${lane.label || lane.id}</span>`;
    }
    const track = document.createElement("div");
    track.className = "lane-track";
    if (!lane.events.length) {
      const empty = document.createElement("span");
      empty.className = "lane-empty";
      empty.textContent = "empty";
      track.append(empty);
    }
    for (const event of lane.events) {
      if (!event.is_timed) continue;
      const start = Math.max(0, event.start_ms || 0);
      const end = Math.max(start + 1000, event.end_ms || start + 1000);
      const el = document.createElement("button");
      el.type = "button";
      const flagClasses = (event.style_flags || []).map(cssToken).filter(Boolean).join(" ");
      el.className = `timeline-event ${cssToken(event.kind || "event")} ${flagClasses} ${cssToken(event.status || "")}`;
      el.style.left = `${(start / scale.duration) * scale.stageWidth}px`;
      el.style.width = `${Math.max(18, ((end - start) / scale.duration) * scale.stageWidth)}px`;
      el.title = `${event.display_title}\n${fmtMs(start)} - ${fmtMs(end)}\n${event.display_meta || shortPath(event.path)}`;
      el.innerHTML = `<span>${event.display_title || "event"}</span><small>${event.display_meta || ""}</small>`;
      track.append(el);
    }
    row.append(label, track);
    els.timeline.append(row);
  }

  const playhead = document.createElement("div");
  playhead.className = "playhead";
  dashboardState.playheadEl = playhead;
  els.timeline.append(playhead);
  updatePlayhead();
}

function syncAxis() {
  els.timeAxis.style.setProperty("--scroll-x", `${els.timelineScroll.scrollLeft}px`);
}

function updatePlayhead() {
  const scale = dashboardState.scale;
  const playhead = dashboardState.playheadEl;
  if (!scale || !playhead) return;
  const ms = livePlayheadMs();
  if (ms === null || ms === undefined) {
    playhead.hidden = true;
    return;
  }
  playhead.hidden = false;
  const left = Math.max(0, Math.min(scale.stageWidth, (ms / scale.duration) * scale.stageWidth));
  playhead.style.left = `${left}px`;
  els.playheadTime.textContent = fmtMs(ms);
  if (dashboardState.follow) {
    const viewportStart = els.timelineScroll.scrollLeft;
    const viewportEnd = viewportStart + els.timelineScroll.clientWidth;
    if (left < viewportStart + 120 || left > viewportEnd - 180) {
      els.timelineScroll.scrollTo({ left: Math.max(0, left - els.timelineScroll.clientWidth * 0.42), behavior: "smooth" });
    }
  }
}

function render() {
  const dashboard = dashboardState.dashboard;
  syncPlayhead(dashboard?.transport || {});
  renderTopline();
  renderList(els.nextList, dashboard.upcoming, "no future song clips");
  renderList(els.commentaryList, dashboard.commentary, "no planned lean-ins");
  renderList(els.automationList, dashboard.automation, "no upcoming automation", 10);
  renderHealth();
  renderSummary();
  renderMixer();
  els.timelineTitle.textContent = dashboard.session?.timeline_mode || "native mix session";
  const signature = eventSignature(dashboard);
  if (signature !== dashboardState.signature) {
    dashboardState.signature = signature;
    renderTimeline();
  } else {
    updatePlayhead();
  }
}

async function readJsonResponse(response) {
  const text = await response.text();
  let payload = null;
  if (text.trim()) {
    try {
      payload = JSON.parse(text);
    } catch (error) {
      const preview = text.trim().slice(0, 180);
      throw new Error(`expected JSON from ${response.url}, got ${response.status} ${response.statusText}: ${preview || error.message}`);
    }
  }
  if (!response.ok) {
    throw new Error(payload?.error || `${response.status} ${response.statusText}`);
  }
  return payload || {};
}

async function refresh() {
  const stateUrl = dashboardState.selectedSet ? `/api/state?set=${encodeURIComponent(dashboardState.selectedSet)}` : "/api/state";
  const response = await fetch(stateUrl, { cache: "no-store" });
  const payload = await readJsonResponse(response);
  dashboardState.payload = payload;
  dashboardState.dashboard = payload.dashboard;
  render();
}

async function refreshSets() {
  const response = await fetch("/api/sets", { cache: "no-store" });
  const payload = await readJsonResponse(response);
  dashboardState.sets = payload.sets || [];
  dashboardState.activeSet = payload.active || null;
  renderArchive();
}

async function postJson(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return readJsonResponse(response);
}

function animatePlayhead() {
  updatePlayhead();
  requestAnimationFrame(animatePlayhead);
}

async function tick() {
  try {
    await refreshSets();
    await refresh();
  } catch (error) {
    els.nowTitle.textContent = "dashboard error";
    els.nowMeta.textContent = error.message;
    els.transportStatus.textContent = "error";
  }
}

els.archiveList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const slug = button.dataset.slug;
  const action = button.dataset.action;
  try {
    els.archiveStatus.textContent = `${action} ${slug}`;
    if (action === "view") {
      dashboardState.selectedSet = slug;
      dashboardState.signature = "";
    } else if (action === "activate") {
      await postJson("/api/sets/activate", { slug, reset_state: true });
      dashboardState.selectedSet = null;
      dashboardState.signature = "";
    } else if (action === "replay") {
      await postJson("/api/sets/replay", { slug, reset_state: true, target: ["all"] });
      dashboardState.selectedSet = null;
      dashboardState.signature = "";
    } else if (action === "render") {
      await postJson("/api/sets/render", { slug, format: "mp3", mp3_bitrate: "128k", keep: 3, max_total_mb: 256 });
    }
    await tick();
  } catch (error) {
    els.archiveStatus.textContent = error.message;
  }
});

els.viewActiveSet.addEventListener("click", async () => {
  dashboardState.selectedSet = null;
  dashboardState.signature = "";
  await tick();
});

els.newSet.addEventListener("click", async () => {
  const title = window.prompt("new set title");
  if (!title) return;
  await postJson("/api/sets/new", { title });
  dashboardState.selectedSet = null;
  dashboardState.signature = "";
  await tick();
});

els.saveLoadedSet.addEventListener("click", async () => {
  try {
    await postJson("/api/sets/save-loaded", {});
    await tick();
  } catch (error) {
    els.archiveStatus.textContent = error.message;
  }
});

els.followPlayhead.addEventListener("change", (event) => {
  dashboardState.follow = event.currentTarget.checked;
  updatePlayhead();
});
els.timelineScroll.addEventListener("scroll", syncAxis, { passive: true });

tick();
setInterval(tick, 3000);
requestAnimationFrame(animatePlayhead);

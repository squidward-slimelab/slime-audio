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
  waveformCache: new Map(),
  feedback: [],
  feedbackTarget: null,
  feedbackRating: "",
  lastMixerFrame: 0,
  lastSetsRefresh: 0,
  tickInFlight: false,
  waveformHydrating: false,
};

const DASHBOARD_POLL_MS = 10000;
const SETS_POLL_MS = 30000;
const WAVEFORM_FETCH_LIMIT = 2;
const MIXER_FRAME_MS = 300;

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
  feedbackForm: document.querySelector("#feedback-form"),
  feedbackTarget: document.querySelector("#feedback-target"),
  feedbackStatus: document.querySelector("#feedback-status"),
  feedbackNote: document.querySelector("#feedback-note"),
  feedbackNow: document.querySelector("#feedback-now"),
  feedbackList: document.querySelector("#feedback-list"),
};

const MIN_STAGE_WIDTH = 1600;
const LANE_LABEL_WIDTH = 104;
const MIXER_DECK_ORDER = ["deck-3", "deck-1", "deck-5", "deck-2", "deck-4"];
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
const AUTOMATION_GRAPH_HEIGHT = 64;
const AUTOMATION_PARAM_META = {
  level: { label: "level", min: 0, max: 1, unit: "", color: "#eef3ef" },
  gain_db: { label: "gain", min: -24, max: 6, unit: "dB", color: "#6fb8e8" },
  trim_db: { label: "trim", min: -12, max: 12, unit: "dB", color: "#82c66f" },
  eq_low_db: { label: "low", min: -12, max: 12, unit: "dB", color: "#e0b85a" },
  eq_mid_db: { label: "mid", min: -12, max: 12, unit: "dB", color: "#b79cf5" },
  eq_high_db: { label: "hi", min: -12, max: 12, unit: "dB", color: "#df7070" },
  lowpass_hz: { label: "lowpass", min: 40, max: 22050, unit: "Hz", color: "#82c66f", scale: "log" },
  highpass_hz: { label: "highpass", min: 20, max: 6000, unit: "Hz", color: "#e0b85a", scale: "log" },
  filter: { label: "filter", min: -1, max: 1, unit: "", color: "#82c66f" },
  duck_volume: { label: "duck", min: 0, max: 1, unit: "", color: "#df7070" },
  position: { label: "xfader", min: -1, max: 1, unit: "", color: "#6fb8e8" },
};
const AUTOMATION_FALLBACK_COLORS = ["#6fb8e8", "#82c66f", "#e0b85a", "#b79cf5", "#df7070"];
const DECK_GRAPH_PARAMS = ["level", "gain_db", "trim_db", "eq_high_db", "eq_mid_db", "eq_low_db", "filter"];

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
      event.stem_indicators,
      event.style_flags,
      event.path,
      event.trim_start_ms,
      event.duration_ms,
      event.target,
      event.owner,
      event.param,
      event.points,
    ])
  );
}

function feedbackEventSnapshot(event) {
  if (!event) return null;
  return {
    id: event.id || null,
    kind: event.kind || null,
    lane: event.lane || null,
    status: event.status || null,
    start_ms: event.start_ms ?? null,
    end_ms: event.end_ms ?? null,
    title: event.display_title || event.title || null,
    meta: event.display_meta || null,
    path: event.path || null,
    target: event.target || null,
    param: event.param || null,
    routine_recipe: event.routine_recipe || null,
  };
}

function feedbackTargetLabel(target) {
  if (!target) return `playhead ${fmtMs(livePlayheadMs())}`;
  const event = target.event || target;
  const title = event.display_title || event.title || event.id || "timeline event";
  const lane = event.lane ? `${event.lane} | ` : "";
  const start = event.start_ms !== null && event.start_ms !== undefined ? fmtMs(event.start_ms) : fmtMs(livePlayheadMs());
  return `${lane}${start} | ${title}`;
}

function currentFeedbackEvent() {
  const target = dashboardState.feedbackTarget?.event;
  if (target) return target;
  const playhead = livePlayheadMs();
  const events = dashboardState.dashboard?.events || [];
  return (
    events.find((event) => event.is_timed && playhead !== null && event.start_ms <= playhead && playhead < event.end_ms && event.kind !== "automation") ||
    dashboardState.dashboard?.now ||
    null
  );
}

function setFeedbackTarget(event = null) {
  dashboardState.feedbackTarget = event ? { event } : null;
  if (els.feedbackTarget) els.feedbackTarget.textContent = feedbackTargetLabel(dashboardState.feedbackTarget);
}

function statusLabel(value) {
  return String(value || "unknown").replace("-", " ");
}

function stemIndicatorTitle(indicator) {
  return `${indicator.name || "stem"} ${statusLabel(indicator.state)}`;
}

function renderStemIndicators(event) {
  const indicators = event.stem_indicators || [];
  if (event.kind !== "stem-group" || !indicators.length) return null;
  const strip = document.createElement("div");
  strip.className = "stem-indicators";
  strip.setAttribute("aria-label", "stem playback state");
  for (const indicator of indicators) {
    const item = document.createElement("i");
    item.className = `stem-indicator ${cssToken(indicator.name || "stem")} ${cssToken(indicator.state || "unknown")}`;
    item.textContent = indicator.label || String(indicator.name || "?").slice(0, 1).toUpperCase();
    item.title = stemIndicatorTitle(indicator);
    strip.append(item);
  }
  return strip;
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

function automationEventsFor(targetId, param) {
  return (dashboardState.dashboard?.events || [])
    .filter((event) => event.kind === "automation" && event.param === param && event.target === targetId)
    .sort((a, b) => numericValue(a.start_ms, 0) - numericValue(b.start_ms, 0));
}

function automationValueFromEvents(events, atMs, fallback = null) {
  let value = fallback;
  for (const event of events || []) {
    const start = numericValue(event.start_ms, null);
    const end = numericValue(event.end_ms, null);
    if (start === null || end === null || atMs === null || atMs === undefined) continue;
    if (atMs >= start && atMs <= end) value = automationValue(event.points, atMs, value);
  }
  return value;
}

function deckAutomationFor(deckId, param, atMs, fallback = null) {
  return automationValueFromEvents(automationEventsFor(deckId, param), atMs, fallback);
}

function automationIsMoving(targetId, param, atMs) {
  return automationEventsFor(targetId, param).some((event) => {
    const points = automationPoints(event);
    for (let index = 1; index < points.length; index += 1) {
      const left = points[index - 1];
      const right = points[index];
      if (atMs >= left.at && atMs <= right.at && Math.abs(right.value - left.value) > 0.0001) return true;
    }
    return false;
  });
}

function automationPoints(event) {
  return (event?.points || [])
    .map((point) => ({ at: numericValue(point.at_ms, null), value: numericValue(point.value, null) }))
    .filter((point) => point.at !== null && point.value !== null)
    .sort((a, b) => a.at - b.at);
}

function eventIds(events) {
  return new Set((events || []).map((event) => event.id).filter(Boolean).map(String));
}

function clipAt(lane, atMs) {
  const clips = (lane.events || [])
    .filter((event) => event.kind !== "automation" && ["song", "effect-track", "vocal"].includes(event.kind))
    .sort((a, b) => numericValue(a.start_ms, 0) - numericValue(b.start_ms, 0));
  return clips.find((event) => atMs >= numericValue(event.start_ms, 0) && atMs < numericValue(event.end_ms, event.start_ms || 0)) || null;
}

function filterValueFromState(lowpass, highpass) {
  if (Number.isFinite(highpass) && highpass > 30) return clamp(highpass / 2500, 0, 1);
  if (Number.isFinite(lowpass) && lowpass > 0 && lowpass < 18_000) return -clamp((18_000 - lowpass) / 18_000, 0, 1);
  return 0;
}

function deckParamValue(lane, param, atMs, directDeckAutomations, legacyAutomations) {
  const clip = clipAt(lane, atMs);
  const clipId = clip?.id;
  const legacyFor = (legacyParam, fallback) => {
    const events = legacyAutomations.filter((event) => event.param === legacyParam && (event.target === clipId || event.owner === clipId));
    return automationValueFromEvents(events, atMs, fallback);
  };
  const deckFor = (deckParam, fallback) => {
    const events = directDeckAutomations.filter((event) => event.param === deckParam);
    return automationValueFromEvents(events, atMs, fallback);
  };
  const trim = numericValue(clip?.trim_db, 0);
  const gain = deckFor("gain_db", legacyFor("gain_db", numericValue(clip?.gain_db, 0)));
  if (param === "level") return clamp((gain + trim + 30) / 42, 0, 1);
  if (param === "gain_db") return gain;
  if (param === "trim_db") return trim;
  if (param === "eq_high_db") return deckFor("eq_high_db", legacyFor("eq_high_db", 0));
  if (param === "eq_mid_db") return deckFor("eq_mid_db", legacyFor("eq_mid_db", 0));
  if (param === "eq_low_db") return deckFor("eq_low_db", legacyFor("eq_low_db", 0));
  if (param === "filter") {
    const lowpass = deckFor("lowpass_hz", legacyFor("lowpass_hz", null));
    const highpass = deckFor("highpass_hz", legacyFor("highpass_hz", null));
    return filterValueFromState(lowpass, highpass);
  }
  return 0;
}

function synthesizeDeckParamAutomation(lane, param, directDeckAutomations, legacyAutomations) {
  if (!lane.id?.startsWith("deck-") || lane.id.endsWith("-fx")) return null;
  const duration = dashboardState.scale?.duration || dashboardState.dashboard?.session?.duration_ms || 60_000;
  const breakpoints = new Set([0, duration]);
  for (const event of lane.events || []) {
    const start = numericValue(event.start_ms, null);
    const end = numericValue(event.end_ms, null);
    if (start !== null) breakpoints.add(clamp(start, 0, duration));
    if (end !== null) breakpoints.add(clamp(end, 0, duration));
  }
  for (const event of [...directDeckAutomations, ...legacyAutomations]) {
    for (const point of automationPoints(event)) breakpoints.add(clamp(point.at, 0, duration));
  }
  const points = [...breakpoints]
    .sort((a, b) => a - b)
    .map((at) => ({ at_ms: at, value: deckParamValue(lane, param, at, directDeckAutomations, legacyAutomations) }));
  if (points.length < 2) return null;
  return {
    kind: "automation",
    target: lane.id,
    param,
    points,
    start_ms: 0,
    end_ms: duration,
    synthetic: "deck-state",
  };
}

function deckGainAutomation(lane, automations) {
  if (!lane.id?.startsWith("deck-") || lane.id.endsWith("-fx")) return null;
  const clips = (lane.events || [])
    .filter((event) => event.kind !== "automation" && event.id && ["song", "effect-track"].includes(event.kind))
    .sort((a, b) => numericValue(a.start_ms, 0) - numericValue(b.start_ms, 0));
  if (!clips.length) return null;
  const clipAutomation = new Map(
    automations
      .filter((event) => event.param === "gain_db" && event.target)
      .map((event) => [String(event.target), event])
  );
  const points = [];
  for (const clip of clips) {
    const start = numericValue(clip.start_ms, null);
    const end = numericValue(clip.end_ms, null);
    if (start === null || end === null) continue;
    const automation = clipAutomation.get(String(clip.id));
    const sourcePoints = automationPoints(automation);
    if (sourcePoints.length) {
      for (const point of sourcePoints) points.push(point);
    } else {
      const value = numericValue(clip.gain_db, 0);
      points.push({ at: start, value }, { at: end, value });
    }
  }
  const deduped = new Map();
  for (const point of points) deduped.set(point.at, point.value);
  const merged = [...deduped.entries()]
    .map(([at, value]) => ({ at_ms: at, value }))
    .sort((a, b) => a.at_ms - b.at_ms);
  if (merged.length < 2) return null;
  return {
    kind: "automation",
    target: lane.id,
    param: "gain_db",
    points: merged,
    start_ms: merged[0].at_ms,
    end_ms: merged[merged.length - 1].at_ms,
    display_title: "gain",
    display_meta: `${lane.id} fader gain`,
    synthetic: "deck-gain",
  };
}

function laneAutomationEvents(lane) {
  const automations = (dashboardState.dashboard?.events || []).filter((event) => event.kind === "automation" && automationPoints(event).length);
  const laneIds = eventIds((lane.events || []).filter((event) => event.kind !== "automation"));
  const allTimelineIds = eventIds((dashboardState.dashboard?.events || []).filter((event) => event.kind !== "automation"));
  if (lane.id === "fader") return automations.filter((event) => event.target === "crossfader");
  if (lane.id === "automation") {
    return automations.filter((event) => event.target !== "crossfader" && !allTimelineIds.has(String(event.target || event.owner || "")));
  }
  const directDeckAutomations = automations.filter((event) => event.target === lane.id);
  const laneAutomations = automations.filter((event) => laneIds.has(String(event.target || "")) || laneIds.has(String(event.owner || "")));
  if (lane.id?.startsWith("deck-") && !lane.id.endsWith("-fx")) {
    return DECK_GRAPH_PARAMS
      .map((param) => synthesizeDeckParamAutomation(lane, param, directDeckAutomations, laneAutomations))
      .filter(Boolean);
  }
  return [...directDeckAutomations, ...laneAutomations.filter((event) => event.param !== "gain_db")];
}

function automationMeta(event, index = 0) {
  const param = event?.param || "automation";
  const configured = AUTOMATION_PARAM_META[param] || {};
  return {
    label: configured.label || param.replace(/_/g, " "),
    min: configured.min,
    max: configured.max,
    unit: configured.unit || "",
    color: configured.color || AUTOMATION_FALLBACK_COLORS[index % AUTOMATION_FALLBACK_COLORS.length],
    scale: configured.scale || "linear",
  };
}

function automationRange(event, points, meta) {
  if (Number.isFinite(meta.min) && Number.isFinite(meta.max)) return [meta.min, meta.max];
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (min === max) return [min - 1, max + 1];
  return [min, max];
}

function automationPct(value, range, meta) {
  const [min, max] = range;
  if (meta.scale === "log") {
    const safeMin = Math.max(1, min);
    const safeMax = Math.max(safeMin + 1, max);
    const safeValue = clamp(value, safeMin, safeMax);
    return clamp((Math.log(safeValue) - Math.log(safeMin)) / (Math.log(safeMax) - Math.log(safeMin)), 0, 1);
  }
  return clamp((value - min) / (max - min), 0, 1);
}

function automationValueText(param, value) {
  if (!Number.isFinite(value)) return "--";
  if (param === "position") {
    if (Math.abs(value) < 0.02) return "center 0.00";
    return `${value < 0 ? "A" : "B"} ${Math.abs(value).toFixed(2)}`;
  }
  if (param === "duck_volume") return `${Math.round(value * 100)}%`;
  if (param?.endsWith("_hz")) return value >= 1000 ? `${(value / 1000).toFixed(2)} kHz` : `${Math.round(value)} Hz`;
  if (param?.endsWith("_db")) return `${value.toFixed(1)} dB`;
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function automationTooltip() {
  let tooltip = document.querySelector(".automation-tooltip");
  if (!tooltip) {
    tooltip = document.createElement("div");
    tooltip.className = "automation-tooltip";
    document.body.append(tooltip);
  }
  return tooltip;
}

function showAutomationTooltip(pointerEvent, lane, automations, scale) {
  if (!automations.length) return;
  const rect = pointerEvent.currentTarget.getBoundingClientRect();
  const atMs = clamp(((pointerEvent.clientX - rect.left) / scale.stageWidth) * scale.duration, 0, scale.duration);
  const activeAutomations = automations.filter((event) => {
    const start = numericValue(event.start_ms, null);
    const end = numericValue(event.end_ms, null);
    return start !== null && end !== null && atMs >= start && atMs <= end;
  });
  if (!activeAutomations.length) {
    hideAutomationTooltip();
    return;
  }
  const tooltip = automationTooltip();
  const head = document.createElement("div");
  head.className = "automation-tooltip-head";
  head.innerHTML = `<strong>${lane.label || lane.id}</strong><span>${fmtMs(atMs)}</span>`;
  const rows = activeAutomations.map((event, index) => {
    const meta = automationMeta(event, index);
    const value = automationValue(event.points, atMs, null);
    const target = event.synthetic === "deck-gain" || event.target === "crossfader" ? "" : event.target ? `${event.target} ` : "";
    const row = document.createElement("div");
    row.className = "automation-tooltip-row";
    row.innerHTML = `<i style="background:${meta.color}"></i><span>${target}${meta.label}</span><strong>${automationValueText(event.param, value)}</strong>`;
    return row;
  });
  tooltip.replaceChildren(head, ...rows);
  tooltip.hidden = false;
  const margin = 14;
  const maxLeft = window.innerWidth - tooltip.offsetWidth - margin;
  const maxTop = window.innerHeight - tooltip.offsetHeight - margin;
  tooltip.style.left = `${clamp(pointerEvent.clientX + margin, margin, Math.max(margin, maxLeft))}px`;
  tooltip.style.top = `${clamp(pointerEvent.clientY + margin, margin, Math.max(margin, maxTop))}px`;
}

function hideAutomationTooltip() {
  const tooltip = document.querySelector(".automation-tooltip");
  if (tooltip) tooltip.hidden = true;
}

function renderAutomationGraph(track, lane, scale) {
  const automations = laneAutomationEvents(lane);
  if (!automations.length) return;
  const graph = document.createElement("div");
  graph.className = "automation-graph";
  graph.style.width = `${scale.stageWidth}px`;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${scale.stageWidth} ${AUTOMATION_GRAPH_HEIGHT}`);
  svg.setAttribute("preserveAspectRatio", "none");
  automations.forEach((event, index) => {
    const points = automationPoints(event);
    if (points.length < 2) return;
    const meta = automationMeta(event, index);
    const range = automationRange(event, points, meta);
    const coordinates = points
      .map((point) => {
        const x = clamp((point.at / scale.duration) * scale.stageWidth, 0, scale.stageWidth);
        const y = AUTOMATION_GRAPH_HEIGHT - automationPct(point.value, range, meta) * (AUTOMATION_GRAPH_HEIGHT - 8) - 4;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", coordinates);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", meta.color);
    line.setAttribute("stroke-width", "2.2");
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(line);
  });
  graph.append(svg);
  track.append(graph);
  track.addEventListener("mousemove", (event) => showAutomationTooltip(event, lane, automations, scale));
  track.addEventListener("mouseleave", hideAutomationTooltip);
}

function waveformKey(event) {
  if (!event?.path || !["song", "effect-track"].includes(event.kind)) return "";
  return JSON.stringify({
    path: event.path,
    trim_start_ms: numericValue(event.trim_start_ms, 0),
    duration_ms: numericValue(event.duration_ms, 0),
    bins: waveformBins(event),
  });
}

function waveformBins(event) {
  const duration = numericValue(event?.duration_ms, 0);
  const scale = dashboardState.scale;
  const width = scale && duration > 0 ? Math.max(18, (duration / scale.duration) * scale.stageWidth) : 180;
  return Math.max(24, Math.min(800, Math.round(width / 3)));
}

function waveformUrl(event) {
  const params = new URLSearchParams({
    path: event.path,
    trim_start_ms: String(numericValue(event.trim_start_ms, 0)),
    bins: String(waveformBins(event)),
  });
  const duration = numericValue(event.duration_ms, 0);
  if (duration > 0) params.set("duration_ms", String(duration));
  return `/api/waveform?${params.toString()}`;
}

function drawWaveform(payload) {
  const wrap = document.createElement("div");
  wrap.className = "timeline-waveform-drawing";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const bands = payload?.bands || {};
  const bandEntries = [
    ["low", bands.low || payload?.peaks || [], "waveform-low"],
    ["mid", bands.mid || [], "waveform-mid"],
    ["high", bands.high || [], "waveform-high"],
  ].filter(([, values]) => values.length);
  const count = Math.max(1, ...bandEntries.map(([, values]) => values.length));
  svg.setAttribute("viewBox", `0 0 ${count} 36`);
  svg.setAttribute("preserveAspectRatio", "none");
  for (const [, bandValues, className] of bandEntries) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    const values = bandValues.map((value) => clamp(numericValue(value, 0), 0, 1));
    const commands = values
      .map((value, index) => {
        const height = Math.max(0.8, value * 16);
        return `M${index + 0.5} ${(18 - height).toFixed(2)}V${(18 + height).toFixed(2)}`;
      })
      .join("");
    path.setAttribute("d", commands);
    path.setAttribute("class", className);
    svg.append(path);
  }
  wrap.append(svg);
  return wrap;
}

function waveformAvailable(payload) {
  return payload?.available && (payload.peaks?.length || payload.bands?.low?.length || payload.bands?.mid?.length || payload.bands?.high?.length);
}

function appendWaveformDrawing(container, payload) {
  container.append(drawWaveform(payload));
}

function renderEventWaveform(container, event) {
  const key = waveformKey(event);
  if (!key) return;
  const waveform = document.createElement("div");
  waveform.className = "timeline-waveform";
  waveform.dataset.waveformKey = key;
  waveform.dataset.waveformUrl = waveformUrl(event);
  const cached = dashboardState.waveformCache.get(key);
  if (waveformAvailable(cached)) appendWaveformDrawing(waveform, cached);
  container.append(waveform);
}

async function hydrateWaveforms() {
  if (dashboardState.waveformHydrating) return;
  const placeholders = [...els.timeline.querySelectorAll(".timeline-waveform[data-waveform-key]")];
  const missing = placeholders.filter((item) => !dashboardState.waveformCache.has(item.dataset.waveformKey));
  dashboardState.waveformHydrating = true;
  try {
    await Promise.all(
      missing.slice(0, WAVEFORM_FETCH_LIMIT).map(async (item) => {
        try {
          const response = await fetch(item.dataset.waveformUrl, { cache: "no-store" });
          const payload = await readJsonResponse(response);
          dashboardState.waveformCache.set(item.dataset.waveformKey, payload);
        } catch (error) {
          dashboardState.waveformCache.set(item.dataset.waveformKey, { available: false, peaks: [], error: error.message });
        }
      })
    );
    for (const item of placeholders) {
      const payload = dashboardState.waveformCache.get(item.dataset.waveformKey);
      if (waveformAvailable(payload) && !item.firstChild) appendWaveformDrawing(item, payload);
    }
  } finally {
    dashboardState.waveformHydrating = false;
  }
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

function clipOrDeckAutomation(deckId, event, param, playhead, fallback = null) {
  const deckValue = deckAutomationFor(deckId, param, playhead, null);
  if (deckValue !== null && deckValue !== undefined) return deckValue;
  return automationFor(event?.id, param, playhead, fallback);
}

function filterState(deckId, event, playhead) {
  const lowpass = clipOrDeckAutomation(deckId, event, "lowpass_hz", playhead, null);
  const highpass = clipOrDeckAutomation(deckId, event, "highpass_hz", playhead, null);
  return filterValueFromState(lowpass, highpass);
}

function channelState(deckId) {
  const playhead = livePlayheadMs();
  const event = pickDeckEvent(deckId, playhead);
  const isMic = deckId === "deck-5";
  const trim = isMic ? 0 : numericValue(event?.trim_db, 0);
  const gain = isMic
    ? (numericValue(event?.volume, 1) - 1) * 12
    : clipOrDeckAutomation(deckId, event, "gain_db", playhead, numericValue(event?.gain_db, 0));
  const eqLow = clipOrDeckAutomation(deckId, event, "eq_low_db", playhead, 0);
  const eqMid = clipOrDeckAutomation(deckId, event, "eq_mid_db", playhead, 0);
  const eqHigh = clipOrDeckAutomation(deckId, event, "eq_high_db", playhead, 0);
  const filter = filterState(deckId, event, playhead);
  const level = clamp((gain + trim + 30) / 42, 0, 1);
  const moving = {
    trim: false,
    hi: automationIsMoving(deckId, "eq_high_db", playhead),
    mid: automationIsMoving(deckId, "eq_mid_db", playhead),
    low: automationIsMoving(deckId, "eq_low_db", playhead),
    filter: automationIsMoving(deckId, "lowpass_hz", playhead) || automationIsMoving(deckId, "highpass_hz", playhead),
    fader: automationIsMoving(deckId, "gain_db", playhead),
    level: automationIsMoving(deckId, "gain_db", playhead),
  };
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
    moving,
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

function renderKnob(def, value, moving = false) {
  const wrap = document.createElement("div");
  wrap.className = `knob-control ${moving ? "moving" : ""}`;
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
    for (const def of KNOB_DEFS) knobs.append(renderKnob(def, channel.values[def.key], channel.moving[def.key]));
    const fader = document.createElement("div");
    fader.className = "channel-fader";
    const level = Math.round(channel.values.level * 100);
    fader.innerHTML = `
      <label>
        <span>level</span>
        <input class="${channel.moving.level ? "moving" : ""}" type="range" min="0" max="100" value="${level}" disabled aria-label="${channel.label} level" />
      </label>
      <label>
        <span>fader</span>
        <input class="${channel.moving.fader ? "moving" : ""}" type="range" min="0" max="100" value="${Math.round(sliderValueFromDb(channel.values.fader))}" disabled aria-label="${channel.label} fader" />
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

function renderFeedback() {
  if (!els.feedbackList) return;
  if (!dashboardState.feedbackTarget) setFeedbackTarget(null);
  els.feedbackList.replaceChildren();
  if (!dashboardState.feedback.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "no feedback logged yet";
    els.feedbackList.append(empty);
    return;
  }
  for (const item of dashboardState.feedback.slice(0, 5)) {
    const row = document.createElement("div");
    row.className = "feedback-row";
    const head = [item.category, item.rating].filter(Boolean).join(" | ");
    const event = item.event || {};
    const target = event.title ? `${fmtMs(item.playhead_ms)} | ${event.title}` : fmtMs(item.playhead_ms);
    row.innerHTML = `<strong>${head || "feedback"}</strong><span>${target}</span><p></p>`;
    row.querySelector("p").textContent = item.note || "";
    els.feedbackList.append(row);
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
    renderAutomationGraph(track, lane, scale);
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
      el.addEventListener("click", () => setFeedbackTarget(event));
      renderEventWaveform(el, event);
      const eventTitle = document.createElement("span");
      eventTitle.textContent = event.display_title || "event";
      const eventMeta = document.createElement("small");
      eventMeta.textContent = event.display_meta || "";
      const stemIndicators = renderStemIndicators(event);
      if (stemIndicators) {
        el.append(eventTitle, stemIndicators, eventMeta);
      } else {
        el.append(eventTitle, eventMeta);
      }
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
  hydrateWaveforms();
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
  renderList(els.nextList, dashboard.upcoming, "no planned timeline events");
  renderList(els.commentaryList, dashboard.commentary, "no planned lean-ins");
  renderList(els.automationList, dashboard.automation, "no upcoming automation", 10);
  renderHealth();
  renderSummary();
  renderMixer();
  renderFeedback();
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

async function refreshFeedback() {
  const response = await fetch("/api/feedback?limit=8", { cache: "no-store" });
  const payload = await readJsonResponse(response);
  dashboardState.feedback = payload.feedback || [];
  renderFeedback();
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
  const now = performance.now();
  if (dashboardState.dashboard && now - dashboardState.lastMixerFrame > MIXER_FRAME_MS) {
    dashboardState.lastMixerFrame = now;
    renderMixer();
  }
  requestAnimationFrame(animatePlayhead);
}

async function tick(options = {}) {
  if (dashboardState.tickInFlight) return;
  dashboardState.tickInFlight = true;
  try {
    const now = Date.now();
    if (options.forceSets || !dashboardState.lastSetsRefresh || now - dashboardState.lastSetsRefresh > SETS_POLL_MS) {
      await refreshSets();
      await refreshFeedback();
      dashboardState.lastSetsRefresh = now;
    }
    await refresh();
  } catch (error) {
    els.nowTitle.textContent = "dashboard error";
    els.nowMeta.textContent = error.message;
    els.transportStatus.textContent = "error";
  } finally {
    dashboardState.tickInFlight = false;
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
    await tick({ forceSets: true });
  } catch (error) {
    els.archiveStatus.textContent = error.message;
  }
});

els.viewActiveSet.addEventListener("click", async () => {
  dashboardState.selectedSet = null;
  dashboardState.signature = "";
  await tick({ forceSets: true });
});

els.newSet.addEventListener("click", async () => {
  const title = window.prompt("new set title");
  if (!title) return;
  await postJson("/api/sets/new", { title });
  dashboardState.selectedSet = null;
  dashboardState.signature = "";
  await tick({ forceSets: true });
});

els.saveLoadedSet.addEventListener("click", async () => {
  try {
    await postJson("/api/sets/save-loaded", {});
    await tick({ forceSets: true });
  } catch (error) {
    els.archiveStatus.textContent = error.message;
  }
});

els.followPlayhead.addEventListener("change", (event) => {
  dashboardState.follow = event.currentTarget.checked;
  updatePlayhead();
});
els.timelineScroll.addEventListener("scroll", syncAxis, { passive: true });

if (els.feedbackNow) {
  els.feedbackNow.addEventListener("click", () => setFeedbackTarget(null));
}

document.querySelectorAll(".feedback-rating button[data-rating]").forEach((button) => {
  button.addEventListener("click", () => {
    const rating = button.dataset.rating || "";
    dashboardState.feedbackRating = dashboardState.feedbackRating === rating ? "" : rating;
    document.querySelectorAll(".feedback-rating button[data-rating]").forEach((item) => {
      item.classList.toggle("selected", item.dataset.rating === dashboardState.feedbackRating);
    });
  });
});

if (els.feedbackForm) {
  els.feedbackForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(els.feedbackForm);
    const feedbackEvent = currentFeedbackEvent();
    const transport = dashboardState.dashboard?.transport || {};
    const playhead = livePlayheadMs();
    const body = {
      category: form.get("category") || "selection",
      rating: dashboardState.feedbackRating || "",
      note: els.feedbackNote.value,
      context: {
        session_path: dashboardState.dashboard?.session_path || "",
        active_set: dashboardState.dashboard?.viewed_set || dashboardState.dashboard?.active_set || {},
        transport: { ...transport, playhead_ms: playhead },
        event: feedbackEventSnapshot(feedbackEvent),
      },
    };
    try {
      els.feedbackStatus.textContent = "sending";
      await postJson("/api/feedback", body);
      els.feedbackNote.value = "";
      dashboardState.feedbackRating = "";
      document.querySelectorAll(".feedback-rating button[data-rating]").forEach((item) => item.classList.remove("selected"));
      els.feedbackStatus.textContent = "logged";
      await refreshFeedback();
    } catch (error) {
      els.feedbackStatus.textContent = error.message;
    }
  });
}

tick({ forceSets: true });
setInterval(tick, DASHBOARD_POLL_MS);
requestAnimationFrame(animatePlayhead);

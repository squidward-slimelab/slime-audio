/* SlimeAudio dashboard.
   Architecture notes for the glitch fixes this rewrite exists for:
   - The timeline (lanes, clips, curves, knobs) is BUILT only when the event
     signature changes. Everything that moves per-frame (playhead, knobs,
     meters, crossfader, LED clock) is updated through CSS custom properties
     on persistent DOM inside one requestAnimationFrame loop — no innerHTML or
     replaceChildren in hot paths.
   - Every rack panel renders only when its own input signature changes, so a
     3s poll that returns identical data touches nothing. */

const state = {
  payload: null,
  dashboard: null,
  sets: [],
  activeSet: null,
  selectedSet: null,
  feedback: [],
  feedbackTarget: null,
  feedbackRating: "",
  playheadSync: null,
  follow: false,
  scale: null,
  waveformCache: new Map(),
  waveformHydrating: false,
  seekDragging: false,
  playheadDragging: false,
  transportBusy: false,
  tickInFlight: false,
  lastSetsRefresh: 0,
  signatures: { timeline: "", panels: new Map() },
};

/* Persistent live-updating DOM registered at timeline build time. */
const live = {
  playheadEl: null,
  knobs: [],        // { el, laneId, def }
  meters: [],       // { el, laneId }
  xfaders: [],      // { el }  (handle elements; --pos)
  laneCtx: new Map(), // laneId -> { lane, deckAutomations, clipAutomations }
  lastKnobFrame: 0,
};

const DASHBOARD_POLL_MS = 3000;
const PLAYHEAD_SNAP_MS = 600;
const SETS_POLL_MS = 30000;
const WAVEFORM_FETCH_LIMIT = 3;
const KNOB_FRAME_MS = 150;
const MIN_STAGE_WIDTH = 1600;
const AUTOMATION_GRAPH_PAD = 5;

const KNOB_MIN_DEG = -135;
const KNOB_MAX_DEG = 135;
const KNOB_DEFS = [
  { key: "gain", label: "gain", param: "gain_db", min: -24, max: 6, ring: "var(--p-gain)" },
  { key: "low", label: "low", param: "eq_low_db", min: -12, max: 12, ring: "var(--p-low)" },
  { key: "mid", label: "mid", param: "eq_mid_db", min: -12, max: 12, ring: "var(--p-mid)" },
  { key: "hi", label: "hi", param: "eq_high_db", min: -12, max: 12, ring: "var(--p-high)" },
  { key: "flt", label: "flt", param: "filter", min: -1, max: 1, ring: "var(--p-lowpass)" },
];

const PARAM_META = {
  level: { label: "level", min: 0, max: 1, unit: "", color: "var(--p-neutral)", raw: "#9aa3ad" },
  gain_db: { label: "gain", min: -24, max: 6, unit: "dB", raw: "#3987e5" },
  trim_db: { label: "trim", min: -12, max: 12, unit: "dB", raw: "#9aa3ad" },
  eq_low_db: { label: "low", min: -12, max: 12, unit: "dB", raw: "#2f9e44" },
  eq_mid_db: { label: "mid", min: -12, max: 12, unit: "dB", raw: "#9085e9" },
  eq_high_db: { label: "hi", min: -12, max: 12, unit: "dB", raw: "#e66767" },
  lowpass_hz: { label: "lowpass", min: 40, max: 22050, unit: "Hz", raw: "#199e70", scale: "log" },
  highpass_hz: { label: "highpass", min: 20, max: 6000, unit: "Hz", raw: "#c98500", scale: "log" },
  filter: { label: "filter", min: -1, max: 1, unit: "", raw: "#199e70" },
  duck_volume: { label: "duck", min: 0, max: 1, unit: "", raw: "#d55181" },
  volume: { label: "volume", min: 0, max: 2, unit: "", raw: "#3987e5" },
  position: { label: "xfader", min: -1, max: 1, unit: "", raw: "#d95926" },
};
const PARAM_FALLBACK = "#9aa3ad";
const DECK_GRAPH_PARAMS = ["level", "gain_db", "trim_db", "eq_high_db", "eq_mid_db", "eq_low_db", "filter"];
const LANE_NAMES = { "deck-5": "mic", effects: "fx", fader: "xfade", automation: "master", actions: "cues" };

const els = {};
for (const id of [
  "now-title", "now-meta", "transport-status", "transport-lamp", "playhead-time", "duration-time",
  "window-time", "updated-time", "transport-play", "transport-pause", "transport-restart",
  "transport-seek", "current-title", "current-state", "current-detail", "session-progress",
  "next-list", "commentary-list", "automation-list", "health-list", "session-summary",
  "archive-banner", "archive-status", "archive-list", "view-active-set", "new-set", "save-loaded-set",
  "timeline-title", "time-axis", "timeline-scroll", "timeline", "follow-playhead",
  "feedback-form", "feedback-target", "feedback-status", "feedback-note", "feedback-now", "feedback-list",
]) {
  els[id.replace(/-([a-z])/g, (_m, c) => c.toUpperCase())] = document.getElementById(id);
}

/* ---------------- helpers ---------------- */

const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
const lerp = (a, b, pct) => a + (b - a) * pct;
const num = (value, fallback = 0) => (Number.isFinite(Number(value)) ? Number(value) : fallback);

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
  const parts = String(path).split("/");
  return parts.slice(Math.max(0, parts.length - 3)).join(" / ");
}

function cssToken(value) {
  return String(value || "").replace(/[^a-z0-9_-]+/gi, "-").replace(/^-+|-+$/g, "").toLowerCase();
}

function statusLabel(value) {
  return String(value || "unknown").replace(/-/g, " ");
}

/* Signature-gated render: a panel only re-renders when its inputs change. */
function renderIfChanged(key, inputs, renderFn) {
  const signature = JSON.stringify(inputs);
  if (state.signatures.panels.get(key) === signature) return;
  state.signatures.panels.set(key, signature);
  renderFn();
}

/* ---------------- playhead anchor ---------------- */

function livePlayheadMs() {
  const sync = state.playheadSync;
  if (!sync) return null;
  if (!sync.playing) return sync.baseMs;
  const advanced = sync.baseMs + Math.max(0, performance.now() - sync.clientMs);
  return Math.min(sync.durationMs || advanced, advanced);
}

function syncPlayhead(transport) {
  const base = transport?.playhead_ms;
  if (base === null || base === undefined) {
    state.playheadSync = null;
    return;
  }
  const playing = Boolean(transport.playing);
  const durationMs = transport.duration_ms || base;
  const current = livePlayheadMs();
  const playingChanged = state.playheadSync?.playing !== playing;
  const drift = current === null ? Infinity : Math.abs(current - base);
  if (!state.playheadSync || playingChanged || drift > PLAYHEAD_SNAP_MS) {
    state.playheadSync = { baseMs: base, clientMs: performance.now(), durationMs, playing };
    return;
  }
  state.playheadSync.durationMs = durationMs;
  state.playheadSync.playing = playing;
}

/* ---------------- automation math ---------------- */

function automationPoints(event) {
  return (event?.points || [])
    .map((point) => ({ at: num(point.at_ms, null), value: num(point.value, null) }))
    .filter((point) => point.at !== null && point.value !== null)
    .sort((a, b) => a.at - b.at);
}

function automationValue(points, atMs, fallback = null) {
  const valid = Array.isArray(points) && points.length && points[0].at !== undefined ? points : automationPoints({ points });
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

function automationValueFromEvents(events, atMs, fallback = null) {
  let value = fallback;
  for (const event of events || []) {
    const start = num(event.start_ms, null);
    const end = num(event.end_ms, null);
    if (start === null || end === null || atMs === null || atMs === undefined) continue;
    if (atMs >= start && atMs <= end) value = automationValue(automationPoints(event), atMs, value);
  }
  return value;
}

function allAutomationEvents() {
  return (state.dashboard?.events || []).filter((event) => event.kind === "automation" && automationPoints(event).length);
}

function automationIsMovingIn(events, atMs) {
  return (events || []).some((event) => {
    const points = automationPoints(event);
    for (let index = 1; index < points.length; index += 1) {
      const left = points[index - 1];
      const right = points[index];
      if (atMs >= left.at && atMs <= right.at && Math.abs(right.value - left.value) > 0.0001) return true;
    }
    return false;
  });
}

function filterValueFromState(lowpass, highpass) {
  if (Number.isFinite(highpass) && highpass > 30) return clamp(highpass / 2500, 0, 1);
  if (Number.isFinite(lowpass) && lowpass > 0 && lowpass < 18_000) return -clamp((18_000 - lowpass) / 18_000, 0, 1);
  return 0;
}

function clipAt(lane, atMs) {
  const clips = (lane.events || [])
    .filter((event) => event.kind !== "automation" && ["song", "stem-group", "effect-track", "vocal"].includes(event.kind))
    .sort((a, b) => num(a.start_ms, 0) - num(b.start_ms, 0));
  return clips.find((event) => atMs >= num(event.start_ms, 0) && atMs < num(event.end_ms, event.start_ms || 0)) || null;
}

function deckParamValue(ctx, param, atMs) {
  const clip = clipAt(ctx.lane, atMs);
  const clipId = clip?.id;
  const legacyFor = (legacyParam, fallback) => {
    const events = ctx.clipAutomations.filter((event) => event.param === legacyParam && (event.target === clipId || event.owner === clipId));
    return automationValueFromEvents(events, atMs, fallback);
  };
  const deckFor = (deckParam, fallback) => {
    const events = ctx.deckAutomations.filter((event) => event.param === deckParam);
    return automationValueFromEvents(events, atMs, fallback);
  };
  const trim = num(clip?.trim_db, 0);
  const gain = deckFor("gain_db", legacyFor("gain_db", num(clip?.gain_db, 0)));
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

function deckParamMoving(ctx, param, atMs) {
  const params = param === "filter" ? ["lowpass_hz", "highpass_hz"] : [param];
  return params.some((name) => automationIsMovingIn(ctx.deckAutomations.filter((event) => event.param === name), atMs));
}

function laneContext(lane) {
  const automations = allAutomationEvents();
  const laneIds = new Set((lane.events || []).filter((event) => event.kind !== "automation").map((event) => String(event.id)).filter(Boolean));
  return {
    lane,
    deckAutomations: automations.filter((event) => event.target === lane.id),
    clipAutomations: automations.filter((event) => laneIds.has(String(event.target || "")) || laneIds.has(String(event.owner || ""))),
  };
}

function synthesizeDeckParamAutomation(ctx, param) {
  const duration = state.scale?.duration || state.dashboard?.session?.duration_ms || 60_000;
  const breakpoints = new Set([0, duration]);
  for (const event of ctx.lane.events || []) {
    const start = num(event.start_ms, null);
    const end = num(event.end_ms, null);
    if (start !== null) breakpoints.add(clamp(start, 0, duration));
    if (end !== null) breakpoints.add(clamp(end, 0, duration));
  }
  for (const event of [...ctx.deckAutomations, ...ctx.clipAutomations]) {
    for (const point of automationPoints(event)) breakpoints.add(clamp(point.at, 0, duration));
  }
  const points = [...breakpoints]
    .sort((a, b) => a - b)
    .map((at) => ({ at_ms: at, value: deckParamValue(ctx, param, at) }));
  if (points.length < 2) return null;
  return { kind: "automation", target: ctx.lane.id, param, points, start_ms: 0, end_ms: duration, synthetic: "deck-state" };
}

function laneAutomationEvents(lane, ctx) {
  const automations = allAutomationEvents();
  const allTimelineIds = new Set(
    (state.dashboard?.events || []).filter((event) => event.kind !== "automation").map((event) => String(event.id)).filter(Boolean)
  );
  if (lane.id === "fader") return automations.filter((event) => event.target === "crossfader");
  if (lane.id === "automation") {
    return automations.filter((event) => event.target !== "crossfader" && !allTimelineIds.has(String(event.target || event.owner || "")));
  }
  if (lane.id?.startsWith("deck-") && !lane.id.endsWith("-fx")) {
    return DECK_GRAPH_PARAMS.map((param) => synthesizeDeckParamAutomation(ctx, param)).filter(Boolean);
  }
  return [...ctx.deckAutomations, ...ctx.clipAutomations.filter((event) => event.param !== "gain_db")];
}

function paramMeta(event, index = 0) {
  const param = event?.param || "automation";
  const configured = PARAM_META[param] || {};
  return {
    label: configured.label || param.replace(/_/g, " "),
    min: configured.min,
    max: configured.max,
    unit: configured.unit || "",
    color: configured.raw || PARAM_FALLBACK,
    scale: configured.scale || "linear",
  };
}

function paramRange(points, meta) {
  if (Number.isFinite(meta.min) && Number.isFinite(meta.max)) return [meta.min, meta.max];
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? [min - 1, max + 1] : [min, max];
}

function paramPct(value, range, meta) {
  const [min, max] = range;
  if (meta.scale === "log") {
    const safeMin = Math.max(1, min);
    const safeMax = Math.max(safeMin + 1, max);
    return clamp((Math.log(clamp(value, safeMin, safeMax)) - Math.log(safeMin)) / (Math.log(safeMax) - Math.log(safeMin)), 0, 1);
  }
  return clamp((value - min) / (max - min), 0, 1);
}

function paramValueText(param, value) {
  if (!Number.isFinite(value)) return "--";
  if (param === "position") {
    if (Math.abs(value) < 0.02) return "center";
    return `${value < 0 ? "A" : "B"} ${Math.abs(value).toFixed(2)}`;
  }
  if (param === "duck_volume" || param === "volume") return `${Math.round(value * 100)}%`;
  if (param?.endsWith("_hz")) return value >= 1000 ? `${(value / 1000).toFixed(2)} kHz` : `${Math.round(value)} Hz`;
  if (param?.endsWith("_db")) return `${value.toFixed(1)} dB`;
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/* ---------------- automation tooltip ---------------- */

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
  const active = automations.filter((event) => {
    const start = num(event.start_ms, null);
    const end = num(event.end_ms, null);
    return start !== null && end !== null && atMs >= start && atMs <= end;
  });
  if (!active.length) {
    hideAutomationTooltip();
    return;
  }
  const tooltip = automationTooltip();
  const head = document.createElement("div");
  head.className = "automation-tooltip-head";
  head.innerHTML = `<strong>${lane.label || lane.id}</strong><span>${fmtMs(atMs)}</span>`;
  const rows = active.map((event, index) => {
    const meta = paramMeta(event, index);
    const value = automationValue(automationPoints(event), atMs, null);
    const target = event.synthetic || event.target === "crossfader" ? "" : event.target ? `${event.target} ` : "";
    const row = document.createElement("div");
    row.className = "automation-tooltip-row";
    row.innerHTML = `<i style="background:${meta.color}"></i><span>${target}${meta.label}</span><strong>${paramValueText(event.param, value)}</strong>`;
    return row;
  });
  tooltip.replaceChildren(head, ...rows);
  tooltip.hidden = false;
  const margin = 14;
  tooltip.style.left = `${clamp(pointerEvent.clientX + margin, margin, Math.max(margin, window.innerWidth - tooltip.offsetWidth - margin))}px`;
  tooltip.style.top = `${clamp(pointerEvent.clientY + margin, margin, Math.max(margin, window.innerHeight - tooltip.offsetHeight - margin))}px`;
}

function hideAutomationTooltip() {
  const tooltip = document.querySelector(".automation-tooltip");
  if (tooltip) tooltip.hidden = true;
}

/* ---------------- waveforms ---------------- */

function waveformBins(event) {
  const duration = num(event?.duration_ms, 0);
  const scale = state.scale;
  const width = scale && duration > 0 ? Math.max(18, (duration / scale.duration) * scale.stageWidth) : 180;
  return Math.max(24, Math.min(800, Math.round(width / 3)));
}

function waveformKey(event) {
  if (!event?.path || !["song", "effect-track"].includes(event.kind)) return "";
  return JSON.stringify({
    path: event.path,
    trim_start_ms: num(event.trim_start_ms, 0),
    duration_ms: num(event.duration_ms, 0),
    bins: waveformBins(event),
  });
}

function waveformUrl(event) {
  const params = new URLSearchParams({
    path: event.path,
    trim_start_ms: String(num(event.trim_start_ms, 0)),
    bins: String(waveformBins(event)),
  });
  const duration = num(event.duration_ms, 0);
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
    const commands = bandValues
      .map((value, index) => {
        const height = Math.max(0.8, clamp(num(value, 0), 0, 1) * 16);
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

function renderEventWaveform(container, event) {
  const key = waveformKey(event);
  if (!key) return;
  const waveform = document.createElement("div");
  waveform.className = "timeline-waveform";
  waveform.dataset.waveformKey = key;
  waveform.dataset.waveformUrl = waveformUrl(event);
  const cached = state.waveformCache.get(key);
  if (waveformAvailable(cached)) waveform.append(drawWaveform(cached));
  container.append(waveform);
}

async function hydrateWaveforms() {
  if (state.waveformHydrating) return;
  const placeholders = [...els.timeline.querySelectorAll(".timeline-waveform[data-waveform-key]")];
  const missing = placeholders.filter((item) => !state.waveformCache.has(item.dataset.waveformKey));
  state.waveformHydrating = true;
  try {
    await Promise.all(
      missing.slice(0, WAVEFORM_FETCH_LIMIT).map(async (item) => {
        try {
          const response = await fetch(item.dataset.waveformUrl, { cache: "no-store" });
          state.waveformCache.set(item.dataset.waveformKey, await readJsonResponse(response));
        } catch (error) {
          state.waveformCache.set(item.dataset.waveformKey, { available: false, peaks: [], error: error.message });
        }
      })
    );
    for (const item of placeholders) {
      const payload = state.waveformCache.get(item.dataset.waveformKey);
      if (waveformAvailable(payload) && !item.firstChild) item.append(drawWaveform(payload));
    }
    if (missing.length > WAVEFORM_FETCH_LIMIT) setTimeout(hydrateWaveforms, 250);
  } finally {
    state.waveformHydrating = false;
  }
}

/* ---------------- timeline build ---------------- */

function timelineScale(durationMs) {
  const duration = Math.max(60_000, durationMs || 60_000);
  const stageWidth = Math.max(MIN_STAGE_WIDTH, Math.ceil(duration / 1000) * 7);
  return { duration, stageWidth };
}

function tickEveryMs(duration) {
  if (duration > 3_600_000) return 900_000;
  if (duration > 900_000) return 300_000;
  return 60_000;
}

function renderAxis(scale) {
  els.timeAxis.replaceChildren();
  const tickEvery = tickEveryMs(scale.duration);
  for (let at = 0; at <= scale.duration; at += tickEvery) {
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.style.left = `${(at / scale.duration) * scale.stageWidth}px`;
    tick.textContent = fmtMs(at);
    els.timeAxis.append(tick);
  }
  syncAxis();
}

function eventSignature(dashboard) {
  return JSON.stringify(
    (dashboard?.events || []).map((event) => [
      event.id, event.kind, event.lane, event.status, event.start_ms, event.end_ms,
      event.display_title, event.display_meta, event.stem_indicators, event.style_flags,
      event.path, event.trim_start_ms, event.duration_ms, event.target, event.owner,
      event.param, event.points,
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

function renderStemIndicators(event) {
  const indicators = event.stem_indicators || [];
  if (event.kind === "song" && event.stems_ready) {
    // Stems exist for this record: beds, bass swaps, and acapella tags are
    // playable moves, not wishes.
    const strip = document.createElement("div");
    strip.className = "stem-indicators";
    const item = document.createElement("i");
    item.className = "stem-indicator ready";
    item.textContent = "S";
    item.title = "stems ready";
    strip.append(item);
    return strip;
  }
  if (event.kind !== "stem-group" || !indicators.length) return null;
  const strip = document.createElement("div");
  strip.className = "stem-indicators";
  strip.setAttribute("aria-label", "stem playback state");
  for (const indicator of indicators) {
    const item = document.createElement("i");
    item.className = `stem-indicator ${cssToken(indicator.name || "stem")} ${cssToken(indicator.state || "unknown")}`;
    item.textContent = indicator.label || String(indicator.name || "?").slice(0, 1).toUpperCase();
    item.title = `${indicator.name || "stem"} ${statusLabel(indicator.state)}`;
    strip.append(item);
  }
  return strip;
}

/* Effects and slips live IN the track lanes: resolve each one to the lane of
   its target clip or deck; anything unresolvable stays on the fx utility lane. */
function resolveChipLane(event, laneByEventId, laneIds) {
  const candidates = [event.target, event.target_clip_id, event.source_clip_id];
  for (const candidate of candidates) {
    if (!candidate) continue;
    const target = String(candidate);
    if (laneByEventId.has(target)) return laneByEventId.get(target);
    if (laneIds.has(target)) return target;
  }
  return null;
}

function chipLabel(event) {
  if (event.kind === "slip") return event.routine_recipe || "slip";
  const type = event.effect_type || "fx";
  const short = { echo: "echo", reverb: "verb", vinyl_brake: "brake" }[type] || type;
  return event.routine_recipe ? `${short}·${event.routine_recipe}` : short;
}

function chipTitle(event) {
  const bits = [`${event.display_title || event.id}`, `${fmtMs(event.start_ms)} - ${fmtMs(event.end_ms)}`];
  if (event.kind === "effect") {
    if (event.wet !== undefined && event.wet !== null) bits.push(`wet ${event.wet}`);
    if (event.delay_ms) bits.push(`delay ${event.delay_ms}ms`);
    if (event.feedback) bits.push(`feedback ${event.feedback}`);
    if (event.preset) bits.push(`preset ${event.preset}`);
  }
  if (event.display_meta) bits.push(event.display_meta);
  return bits.join("\n");
}

function renderChip(event) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = `event-chip ${cssToken(event.kind)} ${cssToken(event.status || "")}`;
  chip.textContent = chipLabel(event);
  chip.title = chipTitle(event);
  chip.addEventListener("click", () => setFeedbackTarget(event));
  return chip;
}

function renderAutomationGraph(track, lane, automations, scale, laneHeight) {
  if (!automations.length) return;
  const graph = document.createElement("div");
  graph.className = "automation-graph";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${scale.stageWidth} ${laneHeight}`);
  svg.setAttribute("preserveAspectRatio", "none");
  automations.forEach((event, index) => {
    const points = automationPoints(event);
    if (points.length < 2) return;
    const meta = paramMeta(event, index);
    const range = paramRange(points, meta);
    const coordinates = points
      .map((point) => {
        const x = clamp((point.at / scale.duration) * scale.stageWidth, 0, scale.stageWidth);
        const y = laneHeight - AUTOMATION_GRAPH_PAD - paramPct(point.value, range, meta) * (laneHeight - AUTOMATION_GRAPH_PAD * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("points", coordinates);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", meta.color);
    line.setAttribute("stroke-width", "1.6");
    line.setAttribute("stroke-linecap", "round");
    line.setAttribute("stroke-linejoin", "round");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    if (event.synthetic) line.setAttribute("stroke-opacity", "0.3");
    svg.append(line);
  });
  graph.append(svg);
  track.append(graph);
  track.addEventListener("mousemove", (event) => showAutomationTooltip(event, lane, automations, scale));
  track.addEventListener("mouseleave", hideAutomationTooltip);
}

function renderKnobRow(laneId) {
  const row = document.createElement("div");
  row.className = "knob-row";
  for (const def of KNOB_DEFS) {
    const wrap = document.createElement("div");
    wrap.className = "knob-control";
    wrap.style.setProperty("--ring", def.ring);
    const knob = document.createElement("div");
    knob.className = "knob";
    const label = document.createElement("span");
    label.textContent = def.label;
    wrap.append(knob, label);
    row.append(wrap);
    live.knobs.push({ el: wrap, knobEl: knob, laneId, def });
  }
  return row;
}

function deckHeader(lane) {
  const header = document.createElement("div");
  header.className = "lane-header";
  const numberMatch = /^deck-(\d+)$/.exec(lane.id || "");
  const isMic = lane.id === "deck-5";
  const routing = state.dashboard?.session?.fader_assignments?.[lane.id] || (isMic ? "THRU" : "");

  const number = document.createElement("span");
  number.className = "deck-number";
  number.textContent = numberMatch ? numberMatch[1] : "";
  const idBlock = document.createElement("div");
  idBlock.className = "lane-id";
  idBlock.innerHTML = `<span class="lane-name">${isMic ? "mic" : "deck"}</span><span class="route-badge route-${cssToken(routing) || "none"}">${routing || "—"}</span>`;

  const strip = document.createElement("div");
  strip.className = "channel-strip";
  strip.append(renderKnobRow(lane.id));
  const meter = document.createElement("div");
  meter.className = "level-meter";
  const fill = document.createElement("i");
  meter.append(fill);
  strip.append(meter);
  live.meters.push({ el: meter, laneId: lane.id });

  header.append(number, idBlock, strip);
  return header;
}

function utilityHeader(lane) {
  const header = document.createElement("div");
  header.className = "lane-header";
  const name = LANE_NAMES[lane.id] || lane.label || lane.id;
  if (lane.id === "fader") {
    const idBlock = document.createElement("div");
    idBlock.className = "lane-id";
    idBlock.innerHTML = `<span class="lane-name">xfade</span>`;
    const strip = document.createElement("div");
    strip.className = "xfade-strip";
    const track = document.createElement("div");
    track.className = "xfade-track";
    const handle = document.createElement("div");
    handle.className = "xfade-handle";
    track.append(handle);
    strip.innerHTML = "<span>A</span>";
    strip.append(track);
    strip.insertAdjacentHTML("beforeend", "<span>B</span>");
    live.xfaders.push({ el: handle });
    header.append(idBlock, strip);
    return header;
  }
  const fxMatch = /^deck-(\d+)-fx$/.exec(lane.id || "");
  header.innerHTML = fxMatch
    ? `<span class="deck-number">${fxMatch[1]}</span><div class="lane-id"><span class="lane-name">fx lane</span></div>`
    : `<div class="lane-id"><span class="lane-name">${name}</span></div>`;
  return header;
}

function renderTimeline() {
  const dashboard = state.dashboard;
  const scale = timelineScale(dashboard?.session?.duration_ms);
  state.scale = scale;
  live.knobs = [];
  live.meters = [];
  live.xfaders = [];
  live.laneCtx = new Map();
  els.timeline.replaceChildren();
  renderAxis(scale);
  const gridPx = (tickEveryMs(scale.duration) / scale.duration) * scale.stageWidth;

  const lanes = dashboard?.lanes || [];
  const laneIds = new Set(lanes.map((lane) => lane.id));
  const laneByEventId = new Map();
  for (const lane of lanes) {
    for (const event of lane.events || []) {
      if (event.kind !== "automation" && event.id) laneByEventId.set(String(event.id), lane.id);
    }
  }
  /* Route effect/slip events into the lane of their target. */
  const chipsByLane = new Map();
  const unresolvedChips = [];
  for (const lane of lanes) {
    if (lane.id !== "effects") continue;
    for (const event of lane.events || []) {
      if (!event.is_timed) continue;
      const targetLane = resolveChipLane(event, laneByEventId, laneIds);
      if (targetLane) {
        if (!chipsByLane.has(targetLane)) chipsByLane.set(targetLane, []);
        chipsByLane.get(targetLane).push(event);
      } else {
        unresolvedChips.push(event);
      }
    }
  }

  for (const lane of lanes) {
    const isDeck = lane.kind === "deck";
    const isFxLane = lane.kind === "effect-lane";
    const isEffectsUtility = lane.id === "effects";
    if (isEffectsUtility && !unresolvedChips.length) continue; /* everything routed into deck lanes */

    const row = document.createElement("div");
    row.className = `lane-row ${isDeck ? "deck" : isFxLane ? "effect-lane" : "utility"}`;
    const ctx = isDeck ? laneContext(lane) : laneContext(lane);
    live.laneCtx.set(lane.id, ctx);
    const header = isDeck ? deckHeader(lane) : utilityHeader(lane);

    const track = document.createElement("div");
    track.className = "lane-track";
    track.style.setProperty("--stage-width", `${scale.stageWidth}px`);
    track.style.setProperty("--grid-px", `${gridPx}px`);

    const laneHeight = isDeck ? 96 : isFxLane ? 30 : 44;
    renderAutomationGraph(track, lane, laneAutomationEvents(lane, ctx), scale, laneHeight);

    const events = isEffectsUtility ? unresolvedChips : lane.events || [];
    let anyEvent = false;
    for (const event of events) {
      if (!event.is_timed || event.kind === "automation") continue;
      anyEvent = true;
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
      const title = document.createElement("span");
      title.textContent = event.display_title || "event";
      const meta = document.createElement("small");
      meta.textContent = event.display_meta || "";
      const stems = renderStemIndicators(event);
      if (stems) el.append(title, stems, meta);
      else el.append(title, meta);
      renderEventWaveform(el, event);
      track.append(el);
    }
    for (const chip of chipsByLane.get(lane.id) || []) {
      const el = renderChip(chip);
      const start = Math.max(0, chip.start_ms || 0);
      const width = Math.max(26, (((chip.end_ms || start + 1000) - start) / scale.duration) * scale.stageWidth);
      el.style.left = `${(start / scale.duration) * scale.stageWidth}px`;
      el.style.width = `${width}px`;
      track.append(el);
      anyEvent = true;
    }
    if (!anyEvent && !laneAutomationEvents(lane, ctx).length) {
      const empty = document.createElement("span");
      empty.className = "lane-empty";
      empty.textContent = "empty";
      track.append(empty);
    }
    row.append(header, track);
    els.timeline.append(row);
  }

  const playhead = document.createElement("div");
  playhead.className = "playhead";
  live.playheadEl = playhead;
  attachPlayheadScrub(playhead);
  els.timeline.append(playhead);
  updatePlayhead();
  updateKnobs(true);
  hydrateWaveforms();
}

/* The playhead IS the scrub control: grab the needle and drag to seek. */
function attachPlayheadScrub(playhead) {
  const msFromPointer = (event) => {
    const rect = els.timelineScroll.getBoundingClientRect();
    const stageX = event.clientX - rect.left + els.timelineScroll.scrollLeft;
    const scale = state.scale;
    if (!scale) return null;
    return clamp((stageX / scale.stageWidth) * scale.duration, 0, scale.duration);
  };
  playhead.addEventListener("pointerdown", (event) => {
    if (!state.scale) return;
    event.preventDefault();
    state.playheadDragging = true;
    playhead.classList.add("dragging");
    playhead.setPointerCapture(event.pointerId);
  });
  playhead.addEventListener("pointermove", (event) => {
    if (!state.playheadDragging) return;
    const ms = msFromPointer(event);
    if (ms === null) return;
    const left = clamp((ms / state.scale.duration) * state.scale.stageWidth, 0, state.scale.stageWidth);
    playhead.style.setProperty("--playhead-x", `${left}px`);
    if (els.playheadTime) els.playheadTime.textContent = fmtMs(ms);
    if (els.transportSeek && !els.transportSeek.disabled) els.transportSeek.value = String(Math.round(ms));
  });
  const finish = (event) => {
    if (!state.playheadDragging) return;
    state.playheadDragging = false;
    playhead.classList.remove("dragging");
    const ms = msFromPointer(event);
    if (ms !== null) sendTransport("seek", { position_ms: Math.round(ms) });
  };
  playhead.addEventListener("pointerup", finish);
  playhead.addEventListener("pointercancel", () => {
    state.playheadDragging = false;
    playhead.classList.remove("dragging");
  });
}

/* ---------------- per-frame updates (CSS vars only) ---------------- */

function syncAxis() {
  els.timeAxis.style.setProperty("--scroll-x", `${els.timelineScroll.scrollLeft}px`);
}

function updatePlayhead() {
  const scale = state.scale;
  const playhead = live.playheadEl;
  if (state.playheadDragging) return; // the hand on the needle wins
  const ms = livePlayheadMs();
  if (els.playheadTime) els.playheadTime.textContent = fmtMs(ms);
  if (!scale || !playhead) return;
  if (ms === null || ms === undefined) {
    playhead.hidden = true;
    return;
  }
  playhead.hidden = false;
  const left = clamp((ms / scale.duration) * scale.stageWidth, 0, scale.stageWidth);
  // The playhead lives inside the scrolled stage: position it in stage
  // coordinates. Subtracting scrollLeft displaced it by the scroll amount,
  // so it vanished the moment the view (or follow mode) scrolled.
  playhead.style.setProperty("--playhead-x", `${left}px`);
  if (!state.seekDragging && els.transportSeek && !els.transportSeek.disabled) {
    els.transportSeek.value = String(Math.round(ms));
  }
  const duration = state.dashboard?.transport?.duration_ms;
  if (els.sessionProgress && duration) {
    els.sessionProgress.style.width = `${clamp((ms / duration) * 100, 0, 100)}%`;
  }
  if (state.follow) {
    const viewportStart = els.timelineScroll.scrollLeft;
    const viewportEnd = viewportStart + els.timelineScroll.clientWidth;
    if (left < viewportStart + 120 || left > viewportEnd - 180) {
      els.timelineScroll.scrollTo({ left: Math.max(0, left - els.timelineScroll.clientWidth * 0.42), behavior: "smooth" });
    }
  }
}

function knobDeg(def, value) {
  const pct = clamp((num(value, 0) - def.min) / (def.max - def.min), 0, 1);
  return lerp(KNOB_MIN_DEG, KNOB_MAX_DEG, pct);
}

function knobValueText(def, value) {
  if (def.key === "flt") {
    if (Math.abs(value) < 0.02) return "center";
    return value < 0 ? `lp ${Math.round(Math.abs(value) * 100)}%` : `hp ${Math.round(value * 100)}%`;
  }
  return `${num(value, 0).toFixed(1)} dB`;
}

function updateKnobs(force = false) {
  const now = performance.now();
  if (!force && now - live.lastKnobFrame < KNOB_FRAME_MS) return;
  live.lastKnobFrame = now;
  const playhead = livePlayheadMs();
  if (playhead === null || playhead === undefined) return;
  for (const knob of live.knobs) {
    const ctx = live.laneCtx.get(knob.laneId);
    if (!ctx) continue;
    const value = deckParamValue(ctx, knob.def.param, playhead);
    knob.knobEl.style.setProperty("--deg", `${knobDeg(knob.def, value)}deg`);
    knob.knobEl.title = `${knob.def.label}: ${knobValueText(knob.def, value)}`;
    knob.el.classList.toggle("moving", deckParamMoving(ctx, knob.def.param === "filter" ? "filter" : knob.def.param, playhead));
  }
  for (const meter of live.meters) {
    const ctx = live.laneCtx.get(meter.laneId);
    if (!ctx) continue;
    meter.el.firstChild.style.setProperty("--level", String(deckParamValue(ctx, "level", playhead)));
  }
  if (live.xfaders.length) {
    const crossfader = allAutomationEvents().filter((event) => event.target === "crossfader" && event.param === "position");
    const position = clamp(automationValueFromEvents(crossfader, playhead, 0) ?? 0, -1, 1);
    for (const fader of live.xfaders) fader.el.style.setProperty("--pos", String(position));
  }
}

function animationFrame() {
  updatePlayhead();
  updateKnobs();
  requestAnimationFrame(animationFrame);
}

/* ---------------- rack panels ---------------- */

function lampClassFor(status) {
  const value = String(status || "").toLowerCase();
  if (["playing", "running", "ok", "true", "live"].includes(value)) return "good";
  if (["paused", "stale", "idle", "waiting"].includes(value)) return "warning";
  if (["stopped", "completed"].includes(value)) return "serious";
  if (["error", "failed", "missing", "dead"].includes(value)) return "critical";
  return "";
}

function renderTopline() {
  const dashboard = state.dashboard;
  const now = dashboard?.now;
  const transport = dashboard?.transport || {};
  const viewedSet = dashboard?.viewed_set;
  const activeSet = dashboard?.active_set;

  els.nowTitle.textContent = viewedSet?.title || now?.display_title || activeSet?.title || "nothing active";
  els.nowMeta.textContent = viewedSet
    ? `archived view | ${viewedSet.slug}`
    : now
      ? now.display_meta || shortPath(now.path)
      : dashboard?.session_path || "waiting for runner state";
  const statusText = viewedSet ? "archived" : statusLabel(transport.status);
  els.transportStatus.textContent = statusText;
  els.transportLamp.className = `lamp ${viewedSet ? "warning" : lampClassFor(transport.status)}`;
  els.transportPlay.classList.toggle("lit", Boolean(transport.playing) && !viewedSet);
  els.durationTime.textContent = fmtMs(transport.duration_ms);
  els.windowTime.textContent = transport.window?.start_ms !== undefined
    ? `${fmtMs(transport.window.start_ms)} - ${fmtMs(transport.window.end_ms)}`
    : "--:--";
  els.updatedTime.textContent = transport.updated_at || state.payload?.generated_at || "--";
  els.archiveBanner.hidden = !viewedSet;

  els.currentTitle.textContent = now?.display_title || "nothing active";
  els.currentDetail.textContent = now
    ? `${fmtMs(now.start_ms)} - ${fmtMs(now.end_ms)} | ${shortPath(now.path || now.source_path)}`
    : "no current load";
  els.currentState.className = `badge ${cssToken(transport.status || "idle")}`;
  els.currentState.textContent = statusLabel(transport.status || "idle");
  renderTransportControls(transport, Boolean(viewedSet));
}

function renderTransportControls(transport, archived = false) {
  const duration = Math.max(0, Number(transport.duration_ms || 0));
  const disabled = archived || state.transportBusy || !duration;
  for (const button of [els.transportPlay, els.transportPause, els.transportRestart]) {
    if (button) button.disabled = disabled;
  }
  if (!els.transportSeek) return;
  els.transportSeek.disabled = disabled;
  els.transportSeek.max = String(Math.max(1, duration));
}

function listItem(event) {
  const item = document.createElement("div");
  item.className = `mini-event ${cssToken(event.kind || "event")} ${cssToken(event.status || "")}`;
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
  const health = state.dashboard?.health || {};
  els.healthList.replaceChildren();
  const rows = [
    ["runner", health.runner_state || "unknown", lampClassFor(health.runner_state)],
    ["window clips", String((health.current_clips || []).length), ""],
    ["receivers", (health.receivers || []).length ? `${health.receivers.length} online` : "not in state", (health.receivers || []).length ? "good" : "warning"],
  ];
  for (const [key, value, lamp] of rows) {
    const row = document.createElement("div");
    row.className = "health-row";
    row.innerHTML = `<span>${key}</span><span class="status-lamp"><i class="lamp ${lamp}"></i><strong>${value}</strong></span>`;
    els.healthList.append(row);
  }
}

function renderSummary() {
  const dashboard = state.dashboard || {};
  const session = dashboard.session || {};
  const counts = session.counts || {};
  const setInfo = dashboard.viewed_set || dashboard.active_set || {};
  const assignments = session.fader_assignments || {};
  const faderText = Object.keys(assignments).length
    ? Object.entries(assignments).map(([deck, side]) => `${deck.replace("deck-", "")}:${side}`).join(" ")
    : "default";
  const rows = [
    ["set", setInfo.title || setInfo.slug || "unassigned"],
    ["mode", session.timeline_mode || "native"],
    ["duration", fmtMs(session.duration_ms)],
    ["actions", counts.action || 0],
    ["songs", counts.song || 0],
    ["stem groups", counts["stem-group"] || 0],
    ["fx clips", counts["effect-track"] || 0],
    ["effects", counts.effect || 0],
    ["slip", counts.slip || 0],
    ["vocal", counts.vocal || 0],
    ["automation", counts.automation || 0],
    ["routing", faderText],
  ];
  els.sessionSummary.replaceChildren();
  for (const [key, value] of rows) {
    const row = document.createElement("div");
    row.className = "summary-row";
    row.innerHTML = `<span>${key}</span><strong>${value}</strong>`;
    els.sessionSummary.append(row);
  }
  const pathRow = document.createElement("div");
  pathRow.className = "summary-row wide";
  pathRow.innerHTML = `<span>session</span><strong title="${dashboard.session_path || ""}">${shortPath(dashboard.session_path) || "--"}</strong>`;
  els.sessionSummary.append(pathRow);
}

function renderArchive() {
  const selected = state.selectedSet;
  const activeSlug = state.activeSet?.slug;
  els.archiveStatus.textContent = selected
    ? `viewing ${selected} — archived session view, playback untouched`
    : activeSlug
      ? `loaded ${activeSlug}`
      : "no active set pointer";
  els.archiveList.replaceChildren();
  if (!state.sets.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "no archived sets yet";
    els.archiveList.append(empty);
    return;
  }
  for (const set of state.sets.slice(0, 12)) {
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

/* ---------------- feedback ---------------- */

function feedbackTargetLabel(target) {
  if (!target) return `playhead ${fmtMs(livePlayheadMs())}`;
  const event = target.event || target;
  const title = event.display_title || event.title || event.id || "timeline event";
  const lane = event.lane ? `${event.lane} | ` : "";
  const start = event.start_ms !== null && event.start_ms !== undefined ? fmtMs(event.start_ms) : fmtMs(livePlayheadMs());
  return `${lane}${start} | ${title}`;
}

function currentFeedbackEvent() {
  const target = state.feedbackTarget?.event;
  if (target) return target;
  const playhead = livePlayheadMs();
  const events = state.dashboard?.events || [];
  return (
    events.find((event) => event.is_timed && playhead !== null && event.start_ms <= playhead && playhead < event.end_ms && event.kind !== "automation") ||
    state.dashboard?.now ||
    null
  );
}

function setFeedbackTarget(event = null) {
  state.feedbackTarget = event ? { event } : null;
  if (els.feedbackTarget) els.feedbackTarget.textContent = feedbackTargetLabel(state.feedbackTarget);
}

function renderFeedback() {
  if (!els.feedbackList) return;
  if (!state.feedbackTarget) setFeedbackTarget(null);
  els.feedbackList.replaceChildren();
  if (!state.feedback.length) {
    const empty = document.createElement("p");
    empty.className = "empty-note";
    empty.textContent = "no feedback logged yet";
    els.feedbackList.append(empty);
    return;
  }
  for (const item of state.feedback.slice(0, 5)) {
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

/* ---------------- render root ---------------- */

function render() {
  const dashboard = state.dashboard;
  syncPlayhead(dashboard?.transport || {});
  renderTopline();
  renderIfChanged("next", dashboard?.upcoming, () => renderList(els.nextList, dashboard.upcoming, "no planned actions or events"));
  renderIfChanged("commentary", dashboard?.commentary, () => renderList(els.commentaryList, dashboard.commentary, "no planned lean-ins"));
  renderIfChanged("automation", dashboard?.automation, () => renderList(els.automationList, dashboard.automation, "no upcoming automation", 10));
  renderIfChanged("health", dashboard?.health, renderHealth);
  renderIfChanged("summary", [dashboard?.session, dashboard?.viewed_set, dashboard?.active_set, dashboard?.session_path], renderSummary);
  renderIfChanged("feedback", [state.feedback], renderFeedback);
  els.timelineTitle.textContent = dashboard?.session?.timeline_mode || "native mix session";
  const signature = eventSignature(dashboard);
  if (signature !== state.signatures.timeline) {
    state.signatures.timeline = signature;
    renderTimeline();
  }
}

/* ---------------- networking ---------------- */

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
  if (!response.ok) throw new Error(payload?.error || `${response.status} ${response.statusText}`);
  return payload || {};
}

async function refresh() {
  const stateUrl = state.selectedSet ? `/api/state?set=${encodeURIComponent(state.selectedSet)}` : "/api/state";
  const response = await fetch(stateUrl, { cache: "no-store" });
  const payload = await readJsonResponse(response);
  state.payload = payload;
  state.dashboard = payload.dashboard;
  render();
}

async function refreshSets() {
  const response = await fetch("/api/sets", { cache: "no-store" });
  const payload = await readJsonResponse(response);
  state.sets = payload.sets || [];
  state.activeSet = payload.active || null;
  renderIfChanged("archive", [state.sets, state.activeSet, state.selectedSet], renderArchive);
}

async function refreshFeedback() {
  const response = await fetch("/api/feedback?limit=8", { cache: "no-store" });
  const payload = await readJsonResponse(response);
  state.feedback = payload.feedback || [];
}

async function postJson(url, body = {}) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return readJsonResponse(response);
}

async function sendTransport(action, extra = {}) {
  if (state.transportBusy) return;
  state.transportBusy = true;
  renderTransportControls(state.dashboard?.transport || {}, Boolean(state.dashboard?.viewed_set));
  try {
    els.transportStatus.textContent = action === "seek" ? "seeking" : action;
    await postJson("/api/transport", { action, target: ["all"], ...extra });
    state.signatures.timeline = "";
    await tick();
  } catch (error) {
    els.transportStatus.textContent = error.message;
    els.transportLamp.className = "lamp critical";
  } finally {
    state.transportBusy = false;
    renderTransportControls(state.dashboard?.transport || {}, Boolean(state.dashboard?.viewed_set));
  }
}

async function tick(options = {}) {
  if (state.tickInFlight) return;
  state.tickInFlight = true;
  try {
    const now = Date.now();
    if (options.forceSets || !state.lastSetsRefresh || now - state.lastSetsRefresh > SETS_POLL_MS) {
      await refreshSets();
      await refreshFeedback();
      state.lastSetsRefresh = now;
    }
    await refresh();
  } catch (error) {
    els.nowTitle.textContent = "dashboard error";
    els.nowMeta.textContent = error.message;
    els.transportStatus.textContent = "error";
    els.transportLamp.className = "lamp critical";
  } finally {
    state.tickInFlight = false;
  }
}

/* ---------------- wiring ---------------- */

els.archiveList.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const slug = button.dataset.slug;
  const action = button.dataset.action;
  try {
    els.archiveStatus.textContent = `${action} ${slug}`;
    if (action === "view") {
      state.selectedSet = slug;
      state.signatures.timeline = "";
    } else if (action === "activate") {
      await postJson("/api/sets/activate", { slug, reset_state: true });
      state.selectedSet = null;
      state.signatures.timeline = "";
    } else if (action === "replay") {
      await postJson("/api/sets/replay", { slug, reset_state: true, target: ["all"] });
      state.selectedSet = null;
      state.signatures.timeline = "";
    } else if (action === "render") {
      await postJson("/api/sets/render", { slug, format: "mp3", mp3_bitrate: "128k", keep: 3, max_total_mb: 256 });
    }
    state.signatures.panels.delete("archive");
    await tick({ forceSets: true });
  } catch (error) {
    els.archiveStatus.textContent = error.message;
  }
});

els.viewActiveSet.addEventListener("click", async () => {
  state.selectedSet = null;
  state.signatures.timeline = "";
  state.signatures.panels.delete("archive");
  await tick({ forceSets: true });
});

els.newSet.addEventListener("click", async () => {
  const title = window.prompt("new set title");
  if (!title) return;
  await postJson("/api/sets/new", { title });
  state.selectedSet = null;
  state.signatures.timeline = "";
  state.signatures.panels.delete("archive");
  await tick({ forceSets: true });
});

els.saveLoadedSet.addEventListener("click", async () => {
  try {
    await postJson("/api/sets/save-loaded", {});
    state.signatures.panels.delete("archive");
    await tick({ forceSets: true });
  } catch (error) {
    els.archiveStatus.textContent = error.message;
  }
});

els.transportPlay.addEventListener("click", () => sendTransport("play"));
els.transportPause.addEventListener("click", () => sendTransport("pause"));
els.transportRestart.addEventListener("click", () => sendTransport("restart"));

els.transportSeek.addEventListener("input", () => {
  state.seekDragging = true;
  els.playheadTime.textContent = fmtMs(Number(els.transportSeek.value));
});
els.transportSeek.addEventListener("change", () => {
  const positionMs = Math.max(0, Number(els.transportSeek.value || 0));
  state.seekDragging = false;
  sendTransport("seek", { position_ms: Math.round(positionMs) });
});
els.transportSeek.addEventListener("pointerdown", () => { state.seekDragging = true; });
els.transportSeek.addEventListener("pointerup", () => { state.seekDragging = false; });

els.followPlayhead.addEventListener("change", (event) => {
  state.follow = event.currentTarget.checked;
  updatePlayhead();
});
els.timelineScroll.addEventListener("scroll", () => {
  syncAxis();
  updatePlayhead();
}, { passive: true });

els.feedbackNow.addEventListener("click", () => setFeedbackTarget(null));

document.querySelectorAll(".feedback-rating button[data-rating]").forEach((button) => {
  button.addEventListener("click", () => {
    const rating = button.dataset.rating || "";
    state.feedbackRating = state.feedbackRating === rating ? "" : rating;
    document.querySelectorAll(".feedback-rating button[data-rating]").forEach((item) => {
      item.classList.toggle("selected", item.dataset.rating === state.feedbackRating);
    });
  });
});

els.feedbackForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(els.feedbackForm);
  const feedbackEvent = currentFeedbackEvent();
  const transport = state.dashboard?.transport || {};
  const playhead = livePlayheadMs();
  const body = {
    category: form.get("category") || "selection",
    rating: state.feedbackRating || "",
    note: els.feedbackNote.value,
    context: {
      session_path: state.dashboard?.session_path || "",
      active_set: state.dashboard?.viewed_set || state.dashboard?.active_set || {},
      transport: { ...transport, playhead_ms: playhead },
      event: feedbackEventSnapshot(feedbackEvent),
    },
  };
  try {
    els.feedbackStatus.textContent = "sending";
    await postJson("/api/feedback", body);
    els.feedbackNote.value = "";
    state.feedbackRating = "";
    document.querySelectorAll(".feedback-rating button[data-rating]").forEach((item) => item.classList.remove("selected"));
    els.feedbackStatus.textContent = "logged";
    await refreshFeedback();
    state.signatures.panels.delete("feedback");
    renderIfChanged("feedback", [state.feedback], renderFeedback);
  } catch (error) {
    els.feedbackStatus.textContent = error.message;
  }
});

tick({ forceSets: true });
setInterval(tick, DASHBOARD_POLL_MS);
requestAnimationFrame(animationFrame);

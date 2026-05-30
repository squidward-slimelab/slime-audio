const state = {
  data: null,
  scale: null,
  timelineSignature: null,
  playhead: null,
  lastActiveNow: null,
  lastActiveAt: 0,
  lastPositionMs: 0,
};

const els = {
  title: document.querySelector("#track-title"),
  meta: document.querySelector("#track-meta"),
  elapsed: document.querySelector("#elapsed"),
  duration: document.querySelector("#duration"),
  scrub: document.querySelector("#scrub-fill"),
  transition: document.querySelector("#transition"),
  timeline: document.querySelector("#timeline"),
  timelineScroll: document.querySelector("#timeline-scroll"),
  timeAxis: document.querySelector("#time-axis"),
  timelineTitle: document.querySelector("#timeline-title"),
  timelineSubtitle: document.querySelector("#timeline-subtitle"),
  summary: document.querySelector("#summary"),
  updated: document.querySelector("#updated"),
  transport: document.querySelector("#transport"),
  currentCardTitle: document.querySelector("#current-card-title"),
  currentCardMeta: document.querySelector("#current-card-meta"),
  upNext: document.querySelector("#up-next"),
  automationList: document.querySelector("#automation-list"),
};

const FALLBACK_TRACK_MS = 180000;
const MIN_STAGE_WIDTH = 1440;
const LANE_LABEL_WIDTH = 96;
const DECK_LANES = ["deck-3", "deck-1", "deck-2", "deck-4"];
const TRANSIENT_EMPTY_MS = 12000;

function fmtMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "--:--";
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function shortPath(path) {
  if (!path) return "";
  const parts = path.split("/");
  return parts.slice(Math.max(0, parts.length - 3)).join(" / ");
}

function parseTimestampMs(value) {
  if (!value) return null;
  const normalized = /[+-]\d{4}$/.test(value) ? `${value.slice(0, -2)}:${value.slice(-2)}` : value;
  const ms = Date.parse(normalized);
  return Number.isNaN(ms) ? null : ms;
}

function activeNow(data = state.data) {
  const now = data?.now || {};
  if (now.track) {
    state.lastActiveNow = now;
    state.lastActiveAt = Date.now();
    return now;
  }
  if (state.lastActiveNow && Date.now() - state.lastActiveAt < TRANSIENT_EMPTY_MS) {
    return state.lastActiveNow;
  }
  return now;
}

function currentEvent(events, data = state.data) {
  const now = activeNow(data);
  return (
    events.find((event) => event.kind === "song" && event.status === "current") ||
    events.find((event) => event.kind === "song" && now?.track?.path && event.path === now.track.path) ||
    null
  );
}

function eventStart(event) {
  return event.start_ms ?? ((event.index ?? 0) * FALLBACK_TRACK_MS);
}

function eventEnd(event) {
  return event.end_ms ?? eventStart(event) + inferredDuration(event);
}

function inferredDuration(event) {
  if (event.duration_ms) return event.duration_ms;
  return FALLBACK_TRACK_MS;
}

function activeElapsedMs(data = state.data) {
  const now = activeNow(data);
  const started = parseTimestampMs(now?.started_at);
  if (started !== null) return Math.max(0, Date.now() - started);
  return Math.max(0, now?.elapsed_ms || 0);
}

function nowPositionMs(data, events) {
  const current = currentEvent(events, data);
  if (!current) return state.lastPositionMs;
  const elapsed = activeElapsedMs(data);
  const position = eventStart(current) + Math.min(elapsed, inferredDuration(current));
  state.lastPositionMs = position;
  return position;
}

function updateNow(data) {
  const now = activeNow(data);
  if (!now.track) {
    els.title.textContent = "nothing playing";
    els.meta.textContent = "waiting for runner state";
    els.elapsed.textContent = "0:00";
    els.duration.textContent = "--:--";
    els.scrub.style.width = "0%";
    els.transition.textContent = "";
    return;
  }
  els.title.textContent = now.track.title || "untitled";
  els.meta.textContent = [now.track.artist, now.track.album].filter(Boolean).join(" - ") || shortPath(now.track.path);
  els.elapsed.textContent = fmtMs(activeElapsedMs(data));
  els.duration.textContent = fmtMs(now.duration_ms);
  const pct = now.duration_ms ? Math.min(100, (activeElapsedMs(data) / now.duration_ms) * 100) : 0;
  els.scrub.style.width = `${pct}%`;
  const transition = now.transition;
  els.transition.textContent = transition
    ? `next: ${shortPath(transition.next)} | key ${transition.key_relation} | pitch ${transition.pitch_shift_semitones >= 0 ? "+" : ""}${transition.pitch_shift_semitones} | tempo ${transition.target_tempo_shift_pct}%`
    : "";
}

function eventLabel(event) {
  if (event.kind === "automation") return `${event.param || "automation"} -> ${event.target || event.owner || "master"}`;
  if (event.kind === "vocal") return event.text || event.id || "vocal drop";
  return event.title || event.id || "song";
}

function eventMeta(event) {
  if (event.kind === "automation") return `${fmtMs(event.start_ms)} - ${fmtMs(event.end_ms)}`;
  if (event.kind === "vocal") return `${fmtMs(event.start_ms)} voice drop`;
  const artistAlbum = [event.artist, event.album].filter(Boolean).join(" - ");
  const time = event.start_ms === null || event.start_ms === undefined ? `slot ${(event.index ?? 0) + 1}` : fmtMs(event.start_ms);
  return `${time} ${artistAlbum}`;
}

function groupEvents(events) {
  const lanes = new Map(DECK_LANES.map((lane) => [lane, []]));
  for (const event of events) {
    const lane = event.deck || event.kind || "timeline";
    if (!lanes.has(lane)) lanes.set(lane, []);
    lanes.get(lane).push(event);
  }
  return lanes;
}

function laneInfo(lane) {
  const match = /^deck-(\d+)$/.exec(lane);
  if (!match) return { number: "", name: lane };
  return { number: match[1], name: "track" };
}

function timelineScale(events) {
  const max = Math.max(60000, ...events.map(eventEnd));
  const stageWidth = Math.max(MIN_STAGE_WIDTH, Math.ceil(max / 1000) * 5);
  return { max, stageWidth };
}

function renderAxis(scale) {
  els.timeAxis.replaceChildren();
  els.timeAxis.style.setProperty("--stage-width", `${scale.stageWidth}px`);
  const tickEvery = scale.max > 3600000 ? 900000 : 300000;
  for (let at = 0; at <= scale.max; at += tickEvery) {
    const tick = document.createElement("span");
    tick.className = "tick";
    tick.style.left = `${LANE_LABEL_WIDTH + (at / scale.max) * scale.stageWidth}px`;
    tick.textContent = fmtMs(at);
    els.timeAxis.append(tick);
  }
  syncAxis();
}

function renderTimeline(events) {
  els.timeline.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "no timeline events yet";
    els.timeline.append(empty);
    return;
  }
  const scale = timelineScale(events);
  state.scale = scale;
  renderAxis(scale);
  els.timeline.style.setProperty("--stage-width", `${scale.stageWidth}px`);
  const lanes = groupEvents(events);
  for (const [lane, items] of lanes.entries()) {
    const row = document.createElement("div");
    row.className = `lane ${DECK_LANES.includes(lane) ? "deck-lane" : "utility-lane"}`;
    const label = document.createElement("div");
    const info = laneInfo(lane);
    label.className = `lane-label ${DECK_LANES.includes(lane) ? "deck-label" : ""}`;
    if (info.number) {
      const number = document.createElement("span");
      number.className = "lane-number";
      number.textContent = info.number;
      const name = document.createElement("span");
      name.className = "lane-name";
      name.textContent = info.name;
      label.append(number, name);
    } else {
      label.textContent = info.name;
    }
    const track = document.createElement("div");
    track.className = `lane-track ${items.length ? "" : "empty-lane"}`;
    if (!items.length) {
      const empty = document.createElement("span");
      empty.className = "empty-lane-label";
      empty.textContent = "empty";
      track.append(empty);
    }
    for (const event of items) {
      const el = document.createElement("div");
      el.className = `event ${event.kind || "event"} ${event.status || ""}`;
      const start = eventStart(event);
      const end = eventEnd(event);
      el.style.left = `${(start / scale.max) * scale.stageWidth}px`;
      el.style.width = `${Math.max(96, ((end - start) / scale.max) * scale.stageWidth)}px`;
      el.title = `${eventLabel(event)}\n${eventMeta(event)}\n${event.path || ""}`;
      if (event.kind !== "automation") {
        const title = document.createElement("div");
        title.className = "event-title";
        title.textContent = eventLabel(event);
        const meta = document.createElement("div");
        meta.className = "event-meta";
        meta.textContent = eventMeta(event);
        el.append(title, meta);
      }
      track.append(el);
    }
    row.append(label, track);
    els.timeline.append(row);
  }
  const playhead = document.createElement("div");
  playhead.className = "playhead";
  state.playhead = playhead;
  els.timeline.append(playhead);
  updatePlayhead();
}

function renderSummary(events) {
  const counts = events.reduce((acc, event) => {
    acc[event.kind] = (acc[event.kind] || 0) + 1;
    return acc;
  }, {});
  els.summary.replaceChildren();
  for (const [key, value] of Object.entries(counts)) {
    const pill = document.createElement("span");
    pill.className = "pill";
    pill.textContent = `${value} ${key}`;
    els.summary.append(pill);
  }
}

function renderInspector(data, events) {
  const songs = events.filter((event) => event.kind === "song");
  const current = currentEvent(songs, data);
  const planned = songs.filter((event) => event.status === "planned").slice(0, 6);
  const automations = events.filter((event) => event.kind === "automation" || event.kind === "vocal").slice(0, 8);

  els.currentCardTitle.textContent = current ? eventLabel(current) : "nothing loaded";
  els.currentCardMeta.textContent = current
    ? `${eventMeta(current)} | ${shortPath(current.path)}`
    : data.session?.path || "";

  renderQueueList(els.upNext, planned, "no future clips");
  renderQueueList(els.automationList, automations, "no automation planned");
}

function renderQueueList(container, events, emptyText) {
  container.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "muted compact";
    empty.textContent = emptyText;
    container.append(empty);
    return;
  }
  for (const event of events) {
    const item = document.createElement("div");
    item.className = `queue-item ${event.kind || ""} ${event.status || ""}`;
    const title = document.createElement("strong");
    title.textContent = eventLabel(event);
    const meta = document.createElement("span");
    meta.textContent = eventMeta(event);
    item.append(title, meta);
    container.append(item);
  }
}

function timelineSignature(events) {
  return JSON.stringify(
    events.map((event) => [
      event.id,
      event.kind,
      event.deck,
      event.index,
      event.status,
      event.start_ms,
      event.end_ms,
      event.duration_ms,
    ])
  );
}

function autoFollowPlayhead(left) {
  if (!els.timelineScroll || !currentEvent(state.data?.session?.events || [])) return;
  const viewportStart = els.timelineScroll.scrollLeft;
  const viewportEnd = viewportStart + els.timelineScroll.clientWidth;
  const target = Math.max(0, left - els.timelineScroll.clientWidth * 0.42);
  if (left > viewportEnd - 180 || left < viewportStart + 120) {
    els.timelineScroll.scrollTo({ left: target, behavior: "smooth" });
  }
}

function syncAxis() {
  els.timeAxis.style.setProperty("--scroll-x", `${els.timelineScroll?.scrollLeft || 0}px`);
}

function render() {
  if (!state.data) return;
  updateNow(state.data);
  const events = state.data.session?.events || [];
  els.timelineTitle.textContent = "session timeline";
  els.timelineSubtitle.textContent = state.data.session?.path || state.data.state_path;
  els.updated.textContent = `updated ${state.data.generated_at}`;
  els.transport.textContent = activeNow(state.data)?.track ? "live playhead active" : "waiting for transport";
  renderSummary(events);
  const signature = timelineSignature(events);
  if (signature !== state.timelineSignature) {
    state.timelineSignature = signature;
    renderTimeline(events);
  } else {
    updatePlayhead();
  }
  renderInspector(state.data, events);
}

function updatePlayhead() {
  const events = state.data?.session?.events || [];
  if (!state.playhead || !state.scale || !events.length) return;
  const position = nowPositionMs(state.data, events);
  const boundedPosition = Math.min(position, state.scale.max);
  const left = (boundedPosition / state.scale.max) * state.scale.stageWidth;
  state.playhead.style.left = `${left}px`;
  autoFollowPlayhead(left);
  updateNow(state.data);
}

async function refresh() {
  const response = await fetch("/api/state", { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  state.data = data;
  render();
}

async function tick() {
  try {
    await refresh();
  } catch (error) {
    els.timeline.replaceChildren();
    const el = document.createElement("div");
    el.className = "error";
    el.textContent = error.message;
    els.timeline.append(el);
  }
}

tick();
setInterval(tick, 3000);
setInterval(updatePlayhead, 1000);
els.timelineScroll.addEventListener("scroll", syncAxis, { passive: true });

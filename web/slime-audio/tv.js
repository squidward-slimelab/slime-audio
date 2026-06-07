const POLL_MS = 5000;
const WAVEFORM_BINS = 420;

const state = {
  dashboard: null,
  payload: null,
  sync: null,
  tickInFlight: false,
  waveformKey: "",
  waveform: null,
  waveformLoading: false,
  lastRenderedNext: "",
  pulse: { low: 0.12, mid: 0.1, high: 0.08 },
};

const els = {
  canvas: document.querySelector("#visualizer"),
  status: document.querySelector("#status-pill"),
  nowTitle: document.querySelector("#now-title"),
  nowMeta: document.querySelector("#now-meta"),
  playheadTime: document.querySelector("#playhead-time"),
  durationTime: document.querySelector("#duration-time"),
  progress: document.querySelector("#session-progress"),
  nextList: document.querySelector("#next-list"),
  setName: document.querySelector("#set-name"),
  windowTime: document.querySelector("#window-time"),
  updatedTime: document.querySelector("#updated-time"),
  runnerState: document.querySelector("#runner-state"),
};

const ctx = els.canvas.getContext("2d", { alpha: false });

function fmtMs(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--:--";
  const total = Math.max(0, Math.floor(Number(value) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function statusLabel(value) {
  return String(value || "idle").replace(/-/g, " ");
}

function shortMeta(event) {
  if (!event) return "no current clip";
  return event.display_meta || event.artist || event.album || event.path || "";
}

function syncPlayhead(transport) {
  const base = transport.playhead_ms;
  if (base === null || base === undefined) {
    state.sync = null;
    return;
  }
  const status = transport.status || "idle";
  const live = livePlayheadMs();
  const shouldReset = !state.sync || live === null || Math.abs(Number(base) - live) > 1500 || state.sync.status !== status;
  if (shouldReset) {
    state.sync = {
      baseMs: Number(base),
      syncedAt: performance.now(),
      status,
      durationMs: transport.duration_ms || Number(base),
    };
  }
  state.sync.durationMs = transport.duration_ms || state.sync.durationMs;
}

function livePlayheadMs() {
  if (!state.sync) return null;
  const elapsed = state.sync.status === "playing" || state.sync.status === "window-active"
    ? performance.now() - state.sync.syncedAt
    : 0;
  return Math.min(state.sync.durationMs || state.sync.baseMs, state.sync.baseMs + elapsed);
}

function currentClipElapsedMs(now, playhead) {
  if (!now || playhead === null || playhead === undefined || now.start_ms === null || now.start_ms === undefined) return 0;
  return Math.max(0, playhead - Number(now.start_ms));
}

function waveformKey(now) {
  if (!now?.path) return "";
  return [now.path, now.trim_start_ms || 0, now.duration_ms || 0].join("|");
}

async function hydrateWaveform(now) {
  const key = waveformKey(now);
  if (!key || key === state.waveformKey || state.waveformLoading) return;
  state.waveformKey = key;
  state.waveform = null;
  state.waveformLoading = true;
  try {
    const params = new URLSearchParams({
      path: now.path,
      trim_start_ms: String(now.trim_start_ms || 0),
      bins: String(WAVEFORM_BINS),
    });
    if (now.duration_ms) params.set("duration_ms", String(now.duration_ms));
    const response = await fetch(`/api/waveform?${params}`, { cache: "no-store" });
    const payload = await readJson(response);
    state.waveform = payload.available ? payload : null;
  } catch {
    state.waveform = null;
  } finally {
    state.waveformLoading = false;
  }
}

function bandAt(name, index) {
  const values = state.waveform?.bands?.[name];
  if (!values?.length) return null;
  const safe = Math.max(0, Math.min(values.length - 1, index));
  return Number(values[safe]) || 0;
}

function fallbackBands(t) {
  return {
    low: 0.3 + Math.sin(t * 0.0017) * 0.16 + Math.sin(t * 0.00043) * 0.1,
    mid: 0.28 + Math.sin(t * 0.0023 + 1.4) * 0.13,
    high: 0.18 + Math.sin(t * 0.0037 + 2.1) * 0.11,
  };
}

function currentBands(now, playhead) {
  const t = performance.now();
  if (!state.waveform?.bands?.low?.length || !now?.duration_ms) {
    const fallback = fallbackBands(t);
    return {
      low: Math.max(0.05, fallback.low),
      mid: Math.max(0.05, fallback.mid),
      high: Math.max(0.04, fallback.high),
      index: Math.floor((t / 80) % WAVEFORM_BINS),
    };
  }
  const elapsed = currentClipElapsedMs(now, playhead);
  const pct = Math.max(0, Math.min(1, elapsed / now.duration_ms));
  const index = Math.floor(pct * (state.waveform.bins || WAVEFORM_BINS));
  return {
    low: bandAt("low", index) ?? 0.1,
    mid: bandAt("mid", index) ?? 0.1,
    high: bandAt("high", index) ?? 0.08,
    index,
  };
}

function easePulse(target) {
  state.pulse.low += (target.low - state.pulse.low) * 0.12;
  state.pulse.mid += (target.mid - state.pulse.mid) * 0.1;
  state.pulse.high += (target.high - state.pulse.high) * 0.09;
}

function resizeCanvas() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.floor(window.innerWidth * dpr);
  const height = Math.floor(window.innerHeight * dpr);
  if (els.canvas.width !== width || els.canvas.height !== height) {
    els.canvas.width = width;
    els.canvas.height = height;
    els.canvas.style.width = `${window.innerWidth}px`;
    els.canvas.style.height = `${window.innerHeight}px`;
  }
  return { width, height, dpr };
}

function drawBackground(width, height, bands) {
  const gradient = ctx.createLinearGradient(0, 0, width, height);
  gradient.addColorStop(0, `rgb(${8 + bands.low * 28}, ${12 + bands.mid * 24}, ${13 + bands.high * 34})`);
  gradient.addColorStop(0.52, `rgb(${6 + bands.high * 22}, ${18 + bands.low * 28}, ${20 + bands.mid * 34})`);
  gradient.addColorStop(1, `rgb(${13 + bands.mid * 24}, ${7 + bands.high * 18}, ${16 + bands.low * 25})`);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, width, height);
}

function drawWaveGrid(width, height, bands, activeIndex) {
  const bins = state.waveform?.bands?.low?.length || WAVEFORM_BINS;
  const centerY = height * 0.51;
  const barWidth = width / bins;
  const now = performance.now();
  ctx.save();
  ctx.globalCompositeOperation = "lighter";
  for (let i = 0; i < bins; i += 1) {
    const low = bandAt("low", i) ?? (0.22 + Math.sin(i * 0.13 + now * 0.002) * 0.12);
    const mid = bandAt("mid", i) ?? (0.18 + Math.sin(i * 0.09 + now * 0.0015) * 0.1);
    const high = bandAt("high", i) ?? (0.12 + Math.sin(i * 0.21 + now * 0.0028) * 0.08);
    const distance = Math.abs(i - activeIndex);
    const focus = Math.max(0, 1 - distance / 38);
    const x = i * barWidth;
    const heightScale = height * (0.13 + focus * 0.26);
    const lowH = Math.max(2, low * heightScale * (1.2 + bands.low));
    const midH = Math.max(1, mid * heightScale * (0.9 + bands.mid));
    const highH = Math.max(1, high * heightScale * (0.7 + bands.high));
    const alpha = 0.12 + focus * 0.72;
    ctx.fillStyle = `rgba(154, 250, 131, ${alpha})`;
    ctx.fillRect(x, centerY - lowH, Math.max(1, barWidth * 0.62), lowH * 2);
    ctx.fillStyle = `rgba(99, 222, 242, ${alpha * 0.82})`;
    ctx.fillRect(x, centerY - midH, Math.max(1, barWidth * 0.45), midH * 2);
    ctx.fillStyle = `rgba(255, 111, 177, ${alpha * 0.68})`;
    ctx.fillRect(x, centerY - highH, Math.max(1, barWidth * 0.28), highH * 2);
  }
  ctx.restore();
}

function drawRings(width, height, bands) {
  const cx = width * 0.68;
  const cy = height * 0.43;
  const max = Math.min(width, height);
  const t = performance.now() * 0.001;
  ctx.save();
  ctx.globalCompositeOperation = "screen";
  for (let i = 0; i < 7; i += 1) {
    const phase = (t * (0.34 + i * 0.035) + i * 0.17) % 1;
    const radius = max * (0.1 + i * 0.045 + phase * 0.05 + bands.low * 0.035);
    ctx.beginPath();
    ctx.ellipse(cx, cy, radius * (1.9 + bands.mid * 0.3), radius * (0.72 + bands.high * 0.25), Math.sin(t * 0.2) * 0.2, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(${120 + i * 14}, ${240 - i * 9}, ${210 + i * 5}, ${0.12 + bands.high * 0.16})`;
    ctx.lineWidth = 1.2 + bands.low * 5;
    ctx.stroke();
  }
  ctx.restore();
}

function drawVignette(width, height) {
  const gradient = ctx.createRadialGradient(width * 0.55, height * 0.45, height * 0.18, width * 0.55, height * 0.45, width * 0.82);
  gradient.addColorStop(0, "rgba(0, 0, 0, 0)");
  gradient.addColorStop(0.72, "rgba(0, 0, 0, 0.22)");
  gradient.addColorStop(1, "rgba(0, 0, 0, 0.72)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, width, height);
}

function drawVisualizer() {
  const { width, height } = resizeCanvas();
  const dashboard = state.dashboard || {};
  const now = dashboard.now;
  const playhead = livePlayheadMs();
  const target = currentBands(now, playhead);
  easePulse(target);
  drawBackground(width, height, state.pulse);
  drawRings(width, height, state.pulse);
  drawWaveGrid(width, height, state.pulse, target.index);
  drawVignette(width, height);
}

function renderNext(events) {
  const visible = (events || []).slice(0, 3);
  const signature = JSON.stringify(visible.map((event) => [event.id, event.start_ms, event.display_title]));
  if (signature === state.lastRenderedNext) return;
  state.lastRenderedNext = signature;
  els.nextList.replaceChildren();
  if (!visible.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "no future song clips";
    els.nextList.append(empty);
    return;
  }
  for (const event of visible) {
    const row = document.createElement("div");
    row.className = "event-row";
    const time = document.createElement("span");
    time.textContent = fmtMs(event.start_ms);
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = event.display_title || "untitled";
    const meta = document.createElement("small");
    meta.textContent = shortMeta(event);
    info.append(title, meta);
    row.append(time, info);
    els.nextList.append(row);
  }
}

function renderStatic() {
  const dashboard = state.dashboard || {};
  const transport = dashboard.transport || {};
  const now = dashboard.now;
  const activeSet = dashboard.viewed_set || dashboard.active_set || state.payload?.active_set || {};

  syncPlayhead(transport);
  hydrateWaveform(now);
  els.status.textContent = statusLabel(transport.status);
  els.status.className = `pill ${String(transport.status || "idle").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
  els.nowTitle.textContent = now?.display_title || activeSet.title || "nothing active";
  els.nowMeta.textContent = now ? shortMeta(now) : dashboard.session_path || "waiting for runner state";
  els.setName.textContent = activeSet.title || activeSet.slug || "active set";
  els.windowTime.textContent = transport.window?.start_ms !== undefined
    ? `${fmtMs(transport.window.start_ms)} - ${fmtMs(transport.window.end_ms)}`
    : "--:--";
  els.updatedTime.textContent = transport.updated_at || state.payload?.generated_at || "--";
  els.runnerState.textContent = dashboard.health?.runner_state || "unknown";
  renderNext(dashboard.upcoming);
}

function renderDynamic() {
  const dashboard = state.dashboard || {};
  const transport = dashboard.transport || {};
  const playhead = livePlayheadMs();
  const duration = transport.duration_ms || dashboard.session?.duration_ms || 0;
  els.playheadTime.textContent = fmtMs(playhead);
  els.durationTime.textContent = fmtMs(duration);
  els.progress.style.width = duration && playhead !== null ? `${Math.max(0, Math.min(100, (playhead / duration) * 100))}%` : "0%";
}

async function readJson(response) {
  const text = await response.text();
  const payload = text.trim() ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(payload.error || `${response.status} ${response.statusText}`);
  return payload;
}

async function tick() {
  if (state.tickInFlight) return;
  state.tickInFlight = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    const payload = await readJson(response);
    state.payload = payload;
    state.dashboard = payload.dashboard;
    renderStatic();
  } catch (error) {
    els.status.textContent = "error";
    els.status.className = "pill error";
    els.nowTitle.textContent = "dashboard error";
    els.nowMeta.textContent = error.message;
  } finally {
    state.tickInFlight = false;
  }
}

function animate() {
  renderDynamic();
  drawVisualizer();
  requestAnimationFrame(animate);
}

window.addEventListener("resize", resizeCanvas, { passive: true });
tick();
setInterval(tick, POLL_MS);
requestAnimationFrame(animate);

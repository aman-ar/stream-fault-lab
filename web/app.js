"use strict";
const $ = id => document.getElementById(id);
let latest = null, online = false, busy = false, paused = false;
let sourceInitialized = false;
let records = [], clearedThrough = 0, lastSequence = 0, renderTimer = null;
let pc = null, resource = null, hls = null, previewGeneration = 0, previewAbort = null;
const opened = new Set();

function badge(id, text, state = "") { $(id).textContent = text; $(id).dataset.state = state; }
function message(text = "") { $("notice").textContent = text; $("notice").hidden = !text; }
function controls() { document.querySelectorAll("[data-control]").forEach(el => { el.disabled = !online || busy; }); }
function rate(value) {
  if (value === null || value === undefined) return "—";
  if (value >= 1e6) return (value / 1e6).toFixed(2) + " Mb/s";
  return (value / 1000).toFixed(1) + " kb/s";
}
async function action(path, data) {
  busy = true; controls(); message();
  try {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The action failed.");
    await refresh();
    return result;
  } catch (error) { message(error.message); return null; }
  finally { busy = false; controls(); }
}
function renderStatus(status) {
  if (!sourceInitialized) { $("source").value = status.source_kind; sourceFields(); sourceInitialized = true; }
  for (const name of ["origin", "edge"]) {
    const component = status.components[name];
    badge(name + "-process", component.process + (component.available ? "" : " · status unavailable"), component.available ? "good" : "bad");
    const paths = component.paths || [];
    $(name + "-stream").textContent = paths.length ? paths.map(p => p.name + ": " + (p.ready ? "ready" : "waiting") + (p.codecs.length ? " · " + p.codecs.join(" / ") : "")).join(" · ") : "No stream status available";
    $(name + "-traffic").textContent = "Path RX " + rate(component.rx_bps) + " · TX " + rate(component.tx_bps) + (name === "edge" ? " · HLS HTTP " + rate(component.hls_http_bps) : "");
  }
  badge("gateway-process", "Running", "good");
  $("gateway-state").textContent = "Listening on port 8080 · " + status.public_host;
  $("run").textContent = "Run " + status.run_id;
  for (const protocol of ["whip", "whep", "hls"]) $(protocol + "-url").value = status.urls[protocol];
  $("sessions").textContent = "Edge WebRTC sessions: " + (status.components.edge.available ? status.components.edge.sessions.length : "—");
  $("bridge").textContent = "HLS audio bridge: " + status.hls_bridge;
  const fault = status.fault;
  $("fault-state").textContent = fault ? fault.preset + " · " + Math.max(0, Math.ceil(fault.expires_at - Date.now() / 1000)) + " seconds remaining" : "No active fault";
  const originReady = status.components.origin.paths.some(p => p.name === "live" && p.ready);
  $("source-state").textContent = "Mode: " + status.source_kind + " · " + (originReady ? "origin stream ready" : "waiting for a source");
}
async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error("Status unavailable");
    const result = await response.json();
    if (latest && latest.run_id !== result.run_id) {
      records = []; lastSequence = 0; clearedThrough = 0; opened.clear(); scheduleLogs();
    }
    latest = result; online = true; badge("connection", "Connected to local lab", "good"); renderStatus(result);
  } catch (_) {
    online = false; badge("connection", "Controller unavailable", "bad");
    for (const name of ["origin", "edge", "gateway"]) badge(name + "-process", "Unknown · connection lost", "bad");
    for (const name of ["origin", "edge"]) { $(name + "-traffic").textContent = "RX — · TX —"; $(name + "-stream").textContent = "Status is stale"; }
    $("fault-state").textContent = "Fault status unknown";
  }
  controls();
}
function sourceFields() {
  const kind = $("source").value;
  for (const name of ["whip", "hls", "demo"]) $(name + "-input").hidden = kind !== name;
}
function faultFields() {
  const preset = $("preset").value, scope = $("scope").value;
  $("scope").disabled = preset === "edge-down";
  $("scope").hidden = preset === "edge-down";
  document.querySelector('label[for="scope"]').hidden = preset === "edge-down";
  $("delay-field").hidden = preset !== "delay";
  let text = "";
  if (preset === "edge-down") text = "Stops only the edge. Current WHEP media breaks; uncached HLS requests fail. The origin and OBS continue running.";
  else if (scope === "whep-post") text = "Existing WebRTC media keeps flowing. Only new WHEP POST requests receive the fault.";
  else if (scope === "hls-playlist") text = "Playlist requests fail or slow down. The player may continue from its existing buffer.";
  else if (scope === "hls-media") text = "Segment and initialization requests fail or slow down. Playlist requests continue.";
  else text = "New WHEP POSTs and all HLS requests receive the fault. Existing WebRTC media keeps flowing.";
  if (preset === "timeout") text += " Matched requests receive no response for up to 60 seconds, even after the fault is restored.";
  $("scope-note").textContent = text;
}
$("source").addEventListener("change", sourceFields);
$("preset").addEventListener("change", faultFields);
$("scope").addEventListener("change", faultFields);
$("configure").addEventListener("click", () => action("/api/source", { kind: $("source").value, url: $("source-url").value }));
$("apply").addEventListener("click", () => action("/api/fault", { preset: $("preset").value, scope: $("scope").value, duration: Number($("duration").value), delay_ms: Number($("delay").value) }));
$("restore").addEventListener("click", () => action("/api/restore", {}));
document.querySelectorAll("[data-copy]").forEach(button => button.addEventListener("click", async () => {
  const field = $(button.dataset.copy);
  try { await navigator.clipboard.writeText(field.value); button.textContent = "Copied"; setTimeout(() => button.textContent = "Copy", 1200); }
  catch (_) { field.select(); message("Copy the selected URL with Command+C."); }
}));

function scheduleLogs() { if (!renderTimer && !paused) renderTimer = setTimeout(() => { renderTimer = null; renderLogs(); }, 300); }
function renderLogs() {
  const component = $("component").value, level = $("level").value, search = $("search").value.trim().toLowerCase();
  const filtered = records.filter(r => r.sequence > clearedThrough && (!component || r.component === component) && (!level || r.level === level) && (!search || JSON.stringify(r).toLowerCase().includes(search)));
  const list = $("records");
  const nearBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 60;
  const oldScroll = list.scrollTop;
  list.replaceChildren();
  for (const record of filtered.slice(-250)) {
    const li = document.createElement("li"), time = document.createElement("time"), name = document.createElement("span"), levelEl = document.createElement("span"), details = document.createElement("details"), summary = document.createElement("summary"), pre = document.createElement("pre");
    time.textContent = record.timestamp.slice(11, 23); time.dateTime = record.timestamp;
    name.textContent = record.component; levelEl.textContent = record.level; levelEl.className = "level"; levelEl.dataset.level = record.level;
    summary.textContent = record.event + " · " + record.message;
    pre.textContent = JSON.stringify(record, null, 2); details.open = opened.has(record.sequence);
    details.addEventListener("toggle", () => { if (details.open) opened.add(record.sequence); else opened.delete(record.sequence); });
    details.append(summary, pre); li.append(time, name, levelEl, details); list.append(li);
  }
  if (!filtered.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = "No matching events."; list.append(li); }
  $("log-count").textContent = "Showing " + Math.min(250, filtered.length) + " of " + filtered.length + " matching events · export includes the retained server trace";
  list.scrollTop = nearBottom ? list.scrollHeight : oldScroll;
}
for (const name of ["component", "level", "search"]) $(name).addEventListener("input", renderLogs);
$("pause").addEventListener("click", () => { paused = !paused; $("pause").textContent = paused ? "Resume view" : "Pause view"; if (!paused) renderLogs(); });
$("clear").addEventListener("click", () => { clearedThrough = lastSequence; opened.clear(); renderLogs(); });
const stream = new EventSource("/api/events");
stream.onmessage = event => {
  const record = JSON.parse(event.data);
  if (latest && record.run_id !== latest.run_id) return;
  if (record.sequence <= lastSequence) return;
  lastSequence = record.sequence; records.push(record);
  if (records.length > 800) { const removed = records.splice(0, records.length - 800); for (const r of removed) opened.delete(r.sequence); }
  scheduleLogs();
};
stream.addEventListener("gap", () => message("Some live events were skipped. Export the trace for the complete retained log."));

async function deleteResource(url) {
  if (!url) return;
  try { await fetch(url, { method: "DELETE", signal: AbortSignal.timeout(3000) }); } catch (_) { /* visible in gateway logs if request reaches it */ }
}
async function stopPreview() {
  previewGeneration++; if (previewAbort) previewAbort.abort(); previewAbort = null;
  if (pc) { pc.close(); pc = null; }
  if (hls) { hls.destroy(); hls = null; }
  const old = resource; resource = null;
  const video = $("video"); video.pause(); video.srcObject = null; video.removeAttribute("src"); video.load();
  badge("player-state", "Stopped"); $("frame-state").textContent = "Rendered frames: —";
  await deleteResource(old);
}
async function waitIce(connection, signal) {
  if (connection.iceGatheringState === "complete") return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error("ICE gathering timed out")), 8000);
    function change() { if (connection.iceGatheringState === "complete") finish(); }
    function abort() { finish(new Error("Preview cancelled")); }
    function finish(error) { clearTimeout(timer); connection.removeEventListener("icegatheringstatechange", change); signal.removeEventListener("abort", abort); error ? reject(error) : resolve(); }
    connection.addEventListener("icegatheringstatechange", change); signal.addEventListener("abort", abort, { once: true });
  });
}
async function playPreview() {
  await stopPreview(); message();
  const generation = previewGeneration, video = $("video"), protocol = $("preview-protocol").value;
  badge("player-state", "Connecting…");
  try {
    if (protocol === "hls") {
      if (video.canPlayType("application/vnd.apple.mpegurl")) video.src = latest.urls.hls;
      else if (window.Hls && Hls.isSupported()) {
        hls = new Hls(); hls.attachMedia(video); hls.loadSource(latest.urls.hls);
        hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal && generation === previewGeneration) badge("player-state", "HLS error: " + data.details, "bad"); });
      } else throw new Error("This browser cannot play HLS. Open the URL in Safari or your player.");
      await video.play();
      if (generation === previewGeneration) badge("player-state", "HLS playing", "good");
    } else {
      previewAbort = new AbortController();
      const timeout = setTimeout(() => previewAbort?.abort(), 20000);
      const connection = new RTCPeerConnection({ iceServers: [] }); pc = connection;
      connection.addTransceiver("video", { direction: "recvonly" }); connection.addTransceiver("audio", { direction: "recvonly" });
      connection.ontrack = event => { if (generation === previewGeneration) { video.srcObject = event.streams[0]; video.play().catch(() => {}); } };
      connection.onconnectionstatechange = () => { if (generation === previewGeneration) badge("player-state", "WebRTC " + connection.connectionState, connection.connectionState === "connected" ? "good" : ""); };
      try {
        await connection.setLocalDescription(await connection.createOffer()); await waitIce(connection, previewAbort.signal);
        const response = await fetch(latest.urls.whep, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: connection.localDescription.sdp, signal: previewAbort.signal });
        if (response.status !== 201) throw new Error("WHEP returned HTTP " + response.status);
        const location = response.headers.get("Location"); if (!location) throw new Error("WHEP response did not contain Location");
        const nextResource = new URL(location, latest.urls.whep).href;
        if (generation !== previewGeneration) { await deleteResource(nextResource); return; }
        resource = nextResource;
        await connection.setRemoteDescription({ type: "answer", sdp: await response.text() });
      } finally { clearTimeout(timeout); }
    }
  } catch (error) {
    if (generation === previewGeneration) {
      // Release a resource created before an SDP negotiation failure.
      const old = resource; resource = null; if (pc) { pc.close(); pc = null; }
      await deleteResource(old); badge("player-state", error.name === "AbortError" ? "Request timed out" : error.message, "bad");
    }
  }
}
$("play").addEventListener("click", playPreview);
$("stop").addEventListener("click", stopPreview);
$("preview-protocol").addEventListener("change", stopPreview);
setInterval(() => {
  if (online && latest?.fault) $("fault-state").textContent = latest.fault.preset + " · " + Math.max(0, Math.ceil(latest.fault.expires_at - Date.now() / 1000)) + " seconds remaining";
  const video = $("video"), quality = video.getVideoPlaybackQuality?.();
  if (quality && (pc || hls || video.currentSrc)) $("frame-state").textContent = "Rendered frames: " + quality.totalVideoFrames + " · dropped: " + quality.droppedVideoFrames + " · playback time: " + video.currentTime.toFixed(1) + "s";
}, 1000);
window.addEventListener("pagehide", () => { if (pc) pc.close(); if (resource) fetch(resource, { method: "DELETE", keepalive: true }).catch(() => {}); });
sourceFields(); faultFields(); controls(); refresh(); setInterval(refresh, 2000);

"use strict";
const $ = id => document.getElementById(id);
let latest = null, online = false, busy = false, paused = false;
let sourceInitialized = false, renderedProtocol = null;
let records = [], clearedThrough = 0, lastSequence = 0, renderTimer = null;
let pc = null, resource = null, hls = null, previewGeneration = 0, previewAbort = null;
const opened = new Set();

function badge(id, text, state = "") { $(id).textContent = text; $(id).dataset.state = state; }
function message(text = "") { $("notice").textContent = text; $("notice").hidden = !text; }
function controls() { document.querySelectorAll("[data-control]").forEach(el => { el.disabled = !online || busy; }); $("scope").disabled = !online || busy || fixedFaultScope(); }

const mediaPresets = ["media-outage", "rtp-stall", "video-stall", "audio-stall", "udp-blocked", "network-quality"];
function fixedFaultScope() { return [...mediaPresets, "edge-down", "origin-down", "session-expired", "ice-unreachable", "dtls-fingerprint", "bad-sdp", "redirect307", "stale-playlist", "truncate-media"].includes($("preset").value); }
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
  const protocol = status.protocol;
  if (renderedProtocol !== protocol) {
    if (renderedProtocol !== null) { stopPreview(); clearedThrough = lastSequence; opened.clear(); scheduleLogs(); }
    renderedProtocol = protocol;
    for (const option of document.querySelectorAll('[data-whep-only]')) { option.hidden = protocol !== "whep"; option.disabled = protocol !== "whep"; }
    for (const option of document.querySelectorAll('[data-hls-only]')) { option.hidden = protocol !== "hls"; option.disabled = protocol !== "hls"; }
    if ($("preset").selectedOptions[0]?.disabled) $("preset").value = "http503";
    if (protocol !== "whep" && ["ice-unreachable", "dtls-fingerprint", "media-outage"].includes($("preset").value)) $("preset").value = "http503";
    const scopes = protocol === "whep" ? [["whep-post", "New WHEP connections (POST)"], ["whep-lifecycle", "WHEP PATCH / DELETE only"]] : [["hls-playlist", "HLS playlists"], ["hls-media", "HLS segments / initialization"], ["playback", "All HLS requests"]];
    $("scope").replaceChildren(...scopes.map(([value, text]) => new Option(text, value)));
    faultFields();
  }
  $("output-protocol").value = protocol;
  $("hls-profile-field").hidden = protocol !== "hls";
  $("hls-profile").value = status.hls_variant || "fmp4";
  $("turn-panel").hidden = protocol !== "whep";
  $("sequence-state").textContent = status.sequence ? "Round " + status.sequence.step + " / " + status.sequence.total + " · " + status.sequence.phase : "No sequence running";
  const turn = status.turn;
  $("turn-state").textContent = turn ? "TURN: " + turn.mode + (turn.media_host ? " · media host " + turn.media_host : "") + (turn.local_running ? " · running" : "") : "TURN off";
  $("whep-output").hidden = protocol !== "whep";
  $("hls-output").hidden = protocol !== "hls";
  $("sessions").hidden = protocol !== "whep";
  $("media-state").hidden = protocol !== "whep";
  $("bridge").hidden = protocol !== "hls";
  $("preview-protocol").textContent = protocol.toUpperCase();
  $("ice-policy").hidden = protocol !== "whep";
  document.querySelector('label[for="ice-policy"]').hidden = protocol !== "whep";
  $("candidate-state").hidden = protocol !== "whep";
  $("delivery-note").textContent = protocol === "whep" ? "WHEP signaling crosses the gateway. WebRTC media crosses the UDP/TCP relay. HLS output is disabled." : "HLS playlists and media cross the gateway. WHEP playback is disabled.";
  if (!sourceInitialized) { $("source").value = status.source_kind; sourceFields(); sourceInitialized = true; }
  for (const name of ["origin", "edge"]) {
    const component = status.components[name];
    badge(name + "-process", component.process + (component.available ? "" : " · status unavailable"), component.available ? "good" : "bad");
    const paths = (component.paths || []).filter(path => protocol === "hls" || path.name !== "live-hls");
    $(name + "-stream").textContent = paths.length ? paths.map(p => p.name + ": " + (p.ready ? "ready" : "waiting") + (p.codecs.length ? " · " + p.codecs.join(" / ") : "")).join(" · ") : "No stream status available";
    $(name + "-traffic").textContent = "Path RX " + rate(component.rx_bps) + " · TX " + rate(component.tx_bps) + (name === "edge" && protocol === "hls" ? " · HLS HTTP " + rate(component.hls_http_bps) : "");
  }
  badge("gateway-process", "Running", "good");
  $("gateway-state").textContent = "Listening on port 8080 · " + status.public_host;
  $("run").textContent = "Run " + status.run_id;
  for (const kind of ["whip", "whep", "hls"]) $(kind + "-url").value = status.urls[kind] || "";
  $("sessions").textContent = "Edge WebRTC sessions: " + (status.components.edge.available ? status.components.edge.sessions.length : "—");
  $("bridge").textContent = "HLS audio bridge: " + status.hls_bridge;
  const relay = status.media_relay;
  $("media-state").textContent = relay ? "Media relay: " + (!relay.running ? "stopped" : relay.impairment ? relay.impairment.mode + " active" : "forwarding") + " · dropped " + (relay.udp_dropped_bytes + relay.tcp_dropped_bytes).toLocaleString() + " bytes this run" : "Media relay: —";
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
    $("media-state").textContent = "Media relay: status unknown";
  }
  controls();
}
function sourceFields() {
  const kind = $("source").value;
  for (const name of ["whip", "hls", "demo"]) $(name + "-input").hidden = kind !== name;
}
function faultFields() {
  const preset = $("preset").value, scope = $("scope").value;
  $("scope").disabled = fixedFaultScope();
  $("scope").hidden = fixedFaultScope();
  document.querySelector('label[for="scope"]').hidden = fixedFaultScope();
  $("delay-field").hidden = preset !== "delay";
  $("direction-field").hidden = !mediaPresets.includes(preset);
  $("quality-fields").hidden = preset !== "network-quality";
  $("retry-field").hidden = !["http429", "http503"].includes(preset);
  $("cut-field").hidden = preset !== "truncate-media";
  let text = "";
  if (preset === "media-outage") text = "Apply while video is playing. Drops WebRTC traffic in both directions over UDP and TCP, including ICE checks and encrypted media. The edge, source and WHEP HTTP stay running. Do not restart the app; watch whether video resumes after restoration. This tests a transport outage; it does not force a DTLS fingerprint error.";
  else if (preset === "ice-unreachable") text = "New WHEP attempts still receive HTTP 201, but UDP and TCP candidates point to endpoints that discard ICE traffic. Existing video continues. Reopen the stream to test. After restoration, a fresh WHEP attempt is needed; previously changed answers stay invalid.";
  else if (preset === "dtls-fingerprint") text = "New WHEP attempts receive HTTP 201 with a valid SDP answer whose certificate fingerprint is wrong. ICE can connect, but DTLS verification should fail. Existing video continues. Reopen the stream to test; recovery needs a fresh healthy WHEP answer.";
  else if (preset === "edge-down") text = renderedProtocol === "hls" ? "Stops only the edge. HLS requests fail; playback continues until the buffer runs out. The origin and source continue running." : "Stops only the edge. Current WebRTC media breaks. The origin and source continue running.";
  else if (["rtp-stall", "video-stall", "audio-stall"].includes(preset)) text = "Apply during playback. Drops the selected RTP media while STUN, DTLS, and RTCP continue. Watch frame/audio progress and stall events; ICE can remain connected.";
  else if (preset === "udp-blocked") text = "Drops all public UDP relay traffic. New attempts may choose direct ICE TCP. TURN/TCP also has a UDP relay-to-edge hop, which this fault blocks. Test TURN transports separately with the reference-player route selector. An existing UDP path may need a new attempt.";
  else if (preset === "network-quality") text = "Impair selected traffic during playback. Inspect loss, jitter, rendered frames, stall duration and recovery. UDP packets can arrive out of order; TCP frames remain ordered.";
  else if (preset === "session-expired") text = "Closes currently active edge read sessions once. The edge process and new WHEP requests stay healthy. Your client must establish a new session to recover.";
  else if (preset === "origin-down") text = "Stops origin and lab source; edge stays running. Generated/pulled sources restart automatically after restoration. OBS may need to reconnect. Existing media and subsequent requests can fail.";
  else if (preset === "stale-playlist") text = "Returns the first successful playlist unchanged with HTTP 200 for the fault interval. LL-HLS reload queries also receive that stale snapshot. Watch live-edge progress and stale-playlist detection.";
  else if (preset === "truncate-media") text = "Closes segment/init responses after sending only the configured portion, retaining the original Content-Length. Watch incomplete-download errors, retries and recovery.";
  else if (preset === "bad-sdp") text = "New WHEP POST returns HTTP 201 with an invalid SDP body. Watch negotiation failure and policy-driven recovery.";
  else if (preset === "redirect307") text = "Redirects new POST requests once to the same lab gateway. The client must preserve POST and its SDP body. This checks redirect handling; it does not emulate a second CDN.";
  else if (scope === "whep-lifecycle") text = "Faults WHEP PATCH / DELETE only. Existing RTP media stays healthy. Recovery and cleanup should handle the response.";
  else if (scope === "whep-post") text = "Existing WebRTC media keeps flowing. Only new WHEP POST requests receive the fault.";
  else if (scope === "hls-playlist") text = "Playlist requests fail or slow down. The player may continue from its existing buffer.";
  else if (scope === "hls-media") text = "Segment and initialization requests fail or slow down. Playlist requests continue.";
  else text = "All HLS playlist and media requests receive the fault.";
  if (preset === "timeout") text += " Matched requests receive no response for up to 60 seconds, even after the fault is restored.";
  $("scope-note").textContent = text;
}
$("source").addEventListener("change", sourceFields);
$("preset").addEventListener("change", () => { faultFields(); controls(); });
$("scope").addEventListener("change", faultFields);
$("configure").addEventListener("click", () => action("/api/source", { kind: $("source").value, url: $("source-url").value }));
$("output-protocol").addEventListener("change", async () => {
  const protocol = $("output-protocol").value;
  busy = true; controls();
  await stopPreview();
  const result = await action("/api/protocol", { protocol });
  if (!result && latest) $("output-protocol").value = latest.protocol;
});
function faultInput() {
  const preset = $("preset").value;
  let scope = $("scope").value, options = {};
  if (mediaPresets.includes(preset) || ["session-expired", "edge-down", "origin-down"].includes(preset)) scope = "playback";
  if (["ice-unreachable", "dtls-fingerprint", "bad-sdp", "redirect307"].includes(preset)) scope = "whep-post";
  if (preset === "stale-playlist") scope = "hls-playlist";
  if (preset === "truncate-media") { scope = "hls-media"; options.cut_percent = Number($("cut").value); }
  if (["http429", "http503"].includes(preset)) options.retry_after = Number($("retry-after").value);
  if (mediaPresets.includes(preset)) options.direction = $("direction").value;
  if (preset === "network-quality") Object.assign(options, { loss_pct: Number($("loss").value), latency_ms: Number($("latency").value), jitter_ms: Number($("jitter").value), bandwidth_kbps: Number($("bandwidth").value), reorder_pct: Number($("reorder").value), burst_packets: Number($("burst").value), seed: 42, include_control: $("control-traffic").value === "all" });
  return { preset, scope, duration: Number($("duration").value), delay_ms: Number($("delay").value), options };
}
$("apply").addEventListener("click", () => action("/api/fault", faultInput()));
$("sequence-start").addEventListener("click", () => {
  const rounds = Number($("rounds").value);
  if (!Number.isInteger(rounds) || rounds < 1 || rounds > 20) { message("Choose 1–20 rounds."); return; }
  action("/api/sequence", { steps: Array.from({ length: rounds }, () => ({ ...faultInput(), healthy: Number($("healthy-gap").value) })) });
});
$("hls-profile").addEventListener("change", async () => { await stopPreview(); action("/api/hls-profile", { variant: $("hls-profile").value }); });
$("turn-mode").addEventListener("change", () => { $("external-turn").hidden = $("turn-mode").value !== "external"; });
$("turn-save").addEventListener("click", async () => {
  let ice_servers = [];
  try { if ($("turn-mode").value === "external") ice_servers = JSON.parse($("turn-servers").value); }
  catch (_) { message("Enter valid JSON for the ICE servers."); return; }
  await stopPreview();
  action("/api/turn", { mode: $("turn-mode").value, media_host: $("media-host").value.trim(), ice_servers });
});
$("turn-copy").addEventListener("click", async () => {
  try { const response = await fetch("/api/ice-servers", { cache: "no-store" }); const config = await response.json(); await navigator.clipboard.writeText(JSON.stringify({ iceServers: config.ice_servers }, null, 2)); message("ICE configuration copied. Keep its credentials private."); }
  catch (_) { message("Could not copy ICE configuration."); }
});
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
  progress = null; dtlsState = ""; lastStats = 0; $("candidate-state").textContent = "Selected ICE pair: —"; $("stall-state").textContent = "Media progress: —";
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
  const generation = previewGeneration, video = $("video"), protocol = latest.protocol;
  badge("player-state", "Connecting…");
  try {
    if (protocol === "hls") {
      if (video.canPlayType("application/vnd.apple.mpegurl")) video.src = previewUrl(latest.urls.hls);
      else if (window.Hls && Hls.isSupported()) {
        hls = new Hls(); hls.attachMedia(video); hls.loadSource(previewUrl(latest.urls.hls));
        hls.on(Hls.Events.ERROR, (_, data) => { playerEvent("hls_error", { details: data.details, fatal: data.fatal }); if (data.fatal && generation === previewGeneration) badge("player-state", "HLS error: " + data.details, "bad"); });
      } else throw new Error("This browser cannot play HLS. Open the URL in Safari or your player.");
      await video.play();
      if (generation === previewGeneration) badge("player-state", "HLS playing", "good");
    } else {
      previewAbort = new AbortController();
      const timeout = setTimeout(() => previewAbort?.abort(), 20000);
      const config = await (await fetch("/api/ice-servers", { cache: "no-store" })).json();
      if (generation !== previewGeneration) return;
      const policy = $("ice-policy").value;
      let servers = config.ice_servers || [];
      if (policy === "relay-udp" || policy === "relay-tcp") servers = servers.map(server => ({ ...server, urls: server.urls.filter(url => policy === "relay-udp" ? url.startsWith("turn:") && url.includes("transport=udp") : url.startsWith("turns:") || url.includes("transport=tcp")) })).filter(server => server.urls.length);
      if (policy !== "all" && !servers.length) throw new Error("Configure TURN before requiring relay playback");
      const connection = new RTCPeerConnection({ iceServers: servers, iceTransportPolicy: policy === "all" ? "all" : "relay" }); pc = connection;
      connection.oniceconnectionstatechange = () => { if (generation === previewGeneration) playerEvent("ice_state", { state: connection.iceConnectionState }); };
      connection.addTransceiver("video", { direction: "recvonly" }); connection.addTransceiver("audio", { direction: "recvonly" });
      connection.ontrack = event => { if (generation === previewGeneration) { video.srcObject = event.streams[0]; video.play().catch(() => {}); } };
      connection.onconnectionstatechange = () => { if (generation === previewGeneration) { badge("player-state", "WebRTC " + connection.connectionState, connection.connectionState === "connected" ? "good" : ""); playerEvent("connection_state", { state: connection.connectionState }); } };
      try {
        await connection.setLocalDescription(await connection.createOffer()); await waitIce(connection, previewAbort.signal);
        const response = await fetch(previewUrl(latest.urls.whep), { method: "POST", headers: { "Content-Type": "application/sdp" }, body: connection.localDescription.sdp, signal: previewAbort.signal });
        if (response.status !== 201) throw new Error("WHEP returned HTTP " + response.status);
        const location = response.headers.get("Location"); if (!location) throw new Error("WHEP response did not contain Location");
        const nextResource = new URL(location, previewUrl(latest.urls.whep)).href;
        if (generation !== previewGeneration) { await deleteResource(nextResource); return; }
        resource = nextResource;
        let answer = await response.text();
        // The reference browser runs on the Mac, where the emulator alias is unavailable.
        answer = answer.replace(/^(a=candidate:\S+\s+\d+\s+(?:udp|tcp)\s+\d+\s+)10\.0\.2\.2(\s+)/gim, (_, prefix, suffix) => prefix + "127.0.0.1" + suffix);
        await connection.setRemoteDescription({ type: "answer", sdp: answer });
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
function previewUrl(value) {
  const url = new URL(value);
  if (url.hostname === "10.0.2.2") url.hostname = "127.0.0.1";
  return url.href;
}
function playerEvent(event, values) {
  fetch("/api/player-event", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ event, ...values }) }).catch(() => {});
}
let progress = null, statsBusy = false, lastStats = 0, dtlsState = "";
async function observePlayer() {
  const video = $("video"), frames = video.getVideoPlaybackQuality?.().totalVideoFrames || 0, now = performance.now();
  if (pc || hls || video.currentSrc) {
    if (!progress) progress = { frames, advanced: false, changed: now, stalled: false };
    if (frames > progress.frames) {
      if (progress.stalled) playerEvent("stall_ended", { frames, duration_ms: Math.round(now - progress.changed) });
      progress = { frames, advanced: true, changed: now, stalled: false };
      $("stall-state").textContent = "Video frames advancing";
    } else if (progress.advanced && !video.paused && now - progress.changed >= 3000) {
      if (!progress.stalled) playerEvent("stall_started", { frames });
      progress.stalled = true;
      $("stall-state").textContent = "Video stalled · " + ((now - progress.changed) / 1000).toFixed(1) + "s";
    }
  }
  if (!pc || statsBusy) return;
  const connection = pc; statsBusy = true;
  try {
    const reports = await connection.getStats(); if (pc !== connection) return;
    let pair = null, incoming = null, audio = null, transport = null;
    reports.forEach(report => { if (report.type === "transport") { transport = report; if (report.selectedCandidatePairId) pair = reports.get(report.selectedCandidatePairId); } if (report.type === "inbound-rtp" && report.kind === "video") incoming = report; if (report.type === "inbound-rtp" && report.kind === "audio") audio = report; });
    if (!pair) reports.forEach(report => { if (report.type === "candidate-pair" && report.nominated && report.state === "succeeded") pair = report; });
    const local = pair && reports.get(pair.localCandidateId), remote = pair && reports.get(pair.remoteCandidateId);
    const stats = { frames, packets_lost: Math.max(0, incoming?.packetsLost || 0), jitter_ms: (incoming?.jitter || 0) * 1000, rtt_ms: (pair?.currentRoundTripTime || 0) * 1000, local_type: local?.candidateType || "", remote_type: remote?.candidateType || "", protocol: local?.protocol || "", relay_protocol: local?.relayProtocol || "", bytes_received: incoming?.bytesReceived || 0, audio_bytes_received: audio?.bytesReceived || 0, concealed_samples: audio?.concealedSamples || 0, turn_used: !!(local?.relayProtocol && /^turns?:/.test(local?.url || "")) };
    $("candidate-state").textContent = local ? "Selected pair: " + (stats.turn_used ? "TURN route · " : "") + local.candidateType + " → " + remote?.candidateType + " · " + local.protocol + (local.relayProtocol ? " · TURN " + local.relayProtocol : "") + " · loss " + stats.packets_lost + " · jitter " + stats.jitter_ms.toFixed(1) + "ms · audio RX " + stats.audio_bytes_received.toLocaleString() + " B" : "Selected ICE pair: waiting";
    if (transport?.dtlsState && transport.dtlsState !== dtlsState) { dtlsState = transport.dtlsState; playerEvent("dtls_state", { state: dtlsState }); }
    if (now - lastStats >= 5000) { playerEvent("stats", stats); lastStats = now; }
  } catch (_) { /* Peer may close while stats are collected. */ }
  finally { statsBusy = false; }
}
setInterval(observePlayer, 1000);
$("play").addEventListener("click", playPreview);
$("stop").addEventListener("click", stopPreview);
setInterval(() => {
  if (online && latest?.fault) $("fault-state").textContent = latest.fault.preset + " · " + Math.max(0, Math.ceil(latest.fault.expires_at - Date.now() / 1000)) + " seconds remaining";
  const video = $("video"), quality = video.getVideoPlaybackQuality?.();
  if (quality && (pc || hls || video.currentSrc)) $("frame-state").textContent = "Rendered frames: " + quality.totalVideoFrames + " · dropped: " + quality.droppedVideoFrames + " · playback time: " + video.currentTime.toFixed(1) + "s";
}, 1000);
window.addEventListener("pagehide", () => { if (pc) pc.close(); if (resource) fetch(resource, { method: "DELETE", keepalive: true }).catch(() => {}); });
sourceFields(); faultFields(); controls(); refresh(); setInterval(refresh, 2000);

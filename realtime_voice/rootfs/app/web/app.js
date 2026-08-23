const byId = id => document.querySelector(`#${id}`);
const statusEl = byId("status");
const ptt = byId("ptt");
const transcript = byId("transcript");
const sink = byId("sink");
const speaker = byId("speaker");
const mode = byId("mode");
const announce = byId("announce");
const progressiveFallback = byId("progressiveFallback");
const saveRoute = byId("saveRoute");
const testRoute = byId("testRoute");
const routeResult = byId("routeResult");
const cancelButton = byId("cancel");
const resetConversation = byId("resetConversation");
const refreshTools = byId("refreshTools");
const catalogSummary = byId("catalogSummary");
const mcpServers = byId("mcpServers");
const sessionSummary = byId("sessionSummary");
const routeSummary = byId("routeSummary");
const phaseSummary = byId("phaseSummary");

function createClientId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  if (typeof crypto.getRandomValues === "function") {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const clientId = localStorage.getItem("voiceClientId") || createClientId();
localStorage.setItem("voiceClientId", clientId);
const speakerModes = JSON.parse(localStorage.getItem("speakerModes") || "{}");
let socket;
let reconnectTimer;
let generation = 0;
let context;
let source;
let capture;
let microphoneStream;
let workletLoaded = false;
let playbackTime = 0;
const playbackNodes = new Set();
let turnStartedAt = 0;

function setPhase(phase, detail = "") {
  phaseSummary.textContent = `Phase: ${phase}${detail ? ` · ${detail}` : ""}`;
  statusEl.textContent = phase[0].toUpperCase() + phase.slice(1);
  statusEl.classList.toggle("ready", ["ready", "listening", "speaking"].includes(phase));
}

function websocketUrl() {
  const base = window.location.href.endsWith("/") ? window.location.href : `${window.location.href}/`;
  const url = new URL("ws", base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url;
}

function selectedRoute() {
  return { sink: sink.value, entity_id: sink.value === "media_player" ? speaker.value || null : null, mode: mode.value, announce: announce.checked, volume: null, progressive_fallback: progressiveFallback.checked };
}

function setRoute(route) {
  sink.value = route.sink;
  mode.value = route.mode;
  announce.checked = route.announce;
  progressiveFallback.checked = route.progressive_fallback ?? true;
  if (route.entity_id) speaker.value = route.entity_id;
  const remote = route.sink === "media_player";
  speaker.disabled = !remote;
  mode.disabled = !remote;
  announce.disabled = !remote;
  progressiveFallback.disabled = !remote;
  routeSummary.textContent = `Current route: ${remote ? route.entity_id || "speaker not selected" : "this browser"} · ${route.mode}`;
}

function setMcpStatus(mcp) {
  catalogSummary.textContent = `Catalog v${mcp.catalog_version}: ${mcp.tool_count} tools`;
  mcpServers.replaceChildren(...mcp.servers.map(server => {
    const item = document.createElement("li");
    const details = [server.status];
    if (server.fallback_active) details.push("fallback endpoint");
    if (server.last_error) details.push(server.last_error);
    if (server.schema_errors.length) details.push(`${server.schema_errors.length} schema errors`);
    item.textContent = `${server.name}: ${details.join(" · ")}`;
    if (server.status !== "connected" || server.schema_errors.length) item.className = "mcp-error";
    return item;
  }));
}

function setSessionDiagnostics(diagnostics) {
  const session = diagnostics.session;
  sessionSummary.textContent = `${diagnostics.session_count} active · age ${session.age_seconds}s · idle ${session.idle_seconds}s · ${session.reconnects} reconnects · ${session.history_turns} turns · ${session.active_tool_calls} tools`;
}

function stopPlayback() {
  for (const node of playbackNodes) try { node.stop(); } catch (_) { /* already stopped */ }
  playbackNodes.clear();
  playbackTime = context?.currentTime || 0;
}

async function playPcm(buffer) {
  if (sink.value !== "browser") return;
  setPhase("speaking");
  context ||= new AudioContext({ sampleRate: 24000 });
  await context.resume();
  const pcm = new Int16Array(buffer);
  const audio = context.createBuffer(1, pcm.length, 24000);
  const channel = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
  const node = context.createBufferSource();
  playbackNodes.add(node);
  node.onended = () => playbackNodes.delete(node);
  node.buffer = audio;
  node.connect(context.destination);
  playbackTime = Math.max(context.currentTime + 0.04, playbackTime);
  node.start(playbackTime);
  playbackTime += audio.duration;
}

async function startCapture() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error("Microphone access requires HTTPS or localhost.");
  context ||= new AudioContext();
  await context.resume();
  if (!workletLoaded) {
    await context.audioWorklet.addModule("static/pcm-worklet.js");
    workletLoaded = true;
  }
  microphoneStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  source = context.createMediaStreamSource(microphoneStream);
  capture = new AudioWorkletNode(context, "pcm-capture");
  capture.port.onmessage = event => {
    if (socket?.readyState === WebSocket.OPEN && ptt.getAttribute("aria-pressed") === "true") socket.send(event.data);
  };
  source.connect(capture);
}

function showError(error) {
  const message = error?.message || error?.type || "Unknown error";
  setPhase("error", message);
  transcript.textContent = `${message} Try again when ready.`;
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  if (!navigator.onLine) return setPhase("offline", "waiting for network");
  reconnectTimer = setTimeout(connect, 2000);
}

function connect() {
  clearTimeout(reconnectTimer);
  if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) return;
  const currentGeneration = ++generation;
  setPhase("connecting");
  const candidate = new WebSocket(websocketUrl());
  socket = candidate;
  candidate.binaryType = "arraybuffer";
  candidate.onopen = () => {
    if (currentGeneration !== generation) return candidate.close();
    candidate.send(JSON.stringify({ type: "hello", protocol: 1, client_id: clientId, name: navigator.userAgent.slice(0, 60) }));
  };
  candidate.onmessage = async event => {
    if (currentGeneration !== generation) return;
    if (event.data instanceof ArrayBuffer) return playPcm(event.data);
    const message = JSON.parse(event.data);
    if (message.type === "session_ready") {
      setPhase("ready");
      const microphoneAvailable = window.isSecureContext && navigator.mediaDevices?.getUserMedia;
      ptt.disabled = !microphoneAvailable;
      for (const control of [saveRoute, testRoute, refreshTools, cancelButton, resetConversation]) control.disabled = false;
      transcript.textContent = microphoneAvailable ? "Hold the button or Space key and speak." : "Microphone access requires HTTPS (or localhost).";
      setRoute(message.route);
      setMcpStatus(message.mcp);
      setSessionDiagnostics(message.diagnostics);
      candidate.send(JSON.stringify({ type: "speakers_list" }));
    } else if (message.type === "speakers") {
      speaker.replaceChildren(...message.items.map(item => new Option(item.name, item.entity_id)));
    } else if (message.type === "route") {
      setRoute(message.route);
      if (message.route.entity_id) {
        speakerModes[message.route.entity_id] = message.route.mode;
        localStorage.setItem("speakerModes", JSON.stringify(speakerModes));
      }
      saveRoute.textContent = "Route saved";
      setTimeout(() => { saveRoute.textContent = "Save output route"; }, 1500);
    } else if (message.type === "route_test_result") {
      routeResult.textContent = message.ok ? `Success: ${message.message} (${message.request_latency_ms} ms request)` : `Failed: ${message.error.message}`;
      testRoute.disabled = false;
    } else if (message.type === "playback_status") {
      const fallback = message.fallback_used ? " · progressive fallback used" : message.fallback ? ` · retrying as ${message.fallback}` : "";
      routeResult.textContent = `${message.mode} playback${message.ok === false ? " failed" : " started"}${fallback}${message.request_latency_ms === undefined ? "" : ` · ${message.request_latency_ms} ms request`}`;
    } else if (message.type === "mcp_status") {
      setMcpStatus(message.mcp);
      refreshTools.disabled = false;
      refreshTools.textContent = "Refresh tools";
    } else if (message.type === "response.created") {
      setPhase("thinking", turnStartedAt ? `${Math.round(performance.now() - turnStartedAt)} ms` : "");
    } else if (message.type === "response.function_call_arguments.done") {
      setPhase("tool use", message.name);
    } else if (message.type === "response.output_audio_transcript.delta") {
      transcript.textContent += message.delta;
    } else if (message.type === "response.done") {
      setPhase("ready");
      transcript.textContent += "\n";
    } else if (message.type === "session_diagnostics") {
      setSessionDiagnostics(message);
    } else if (message.type === "session.reconnecting") {
      setPhase("reconnecting");
    } else if (message.type === "session.reconnected") {
      setPhase("ready", `${message.reconnects} reconnects`);
    } else if (message.type === "session.expired") {
      setPhase("idle timeout");
      transcript.textContent = "Session expired after inactivity. Reconnecting…";
    } else if (message.type === "conversation_reset") {
      transcript.textContent = "Conversation reset.";
      setPhase("ready");
    } else if (message.type === "error" || message.type === "app.error") {
      showError(message.error);
    }
  };
  candidate.onerror = () => showError({ message: "Unable to reach the voice service." });
  candidate.onclose = () => {
    if (currentGeneration !== generation) return;
    stopPlayback();
    ptt.disabled = true;
    for (const control of [refreshTools, cancelButton, resetConversation]) control.disabled = true;
    setPhase("disconnected");
    scheduleReconnect();
  };
}

async function beginTalk() {
  if (ptt.disabled || ptt.getAttribute("aria-pressed") === "true") return;
  try {
    if (!capture) await startCapture();
  } catch (error) {
    capture = null;
    showError({ message: error.name === "NotAllowedError" ? "Microphone permission was denied. Allow microphone access in browser site settings, then try again." : error.message });
    return;
  }
  stopPlayback();
  turnStartedAt = performance.now();
  ptt.classList.add("active");
  ptt.setAttribute("aria-pressed", "true");
  ptt.textContent = "Listening…";
  transcript.textContent = "";
  setPhase("listening");
  socket.send(JSON.stringify({ type: "ptt_start" }));
}

function endTalk() {
  if (ptt.getAttribute("aria-pressed") !== "true") return;
  ptt.classList.remove("active");
  ptt.setAttribute("aria-pressed", "false");
  ptt.textContent = "Hold to talk";
  setPhase("thinking");
  socket.send(JSON.stringify({ type: "ptt_stop" }));
}

ptt.addEventListener("pointerdown", event => { event.preventDefault(); beginTalk(); });
for (const eventName of ["pointerup", "pointercancel", "pointerleave"]) ptt.addEventListener(eventName, endTalk);
document.addEventListener("keydown", event => {
  if (event.code === "Space" && !event.repeat && !["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(event.target.tagName)) {
    event.preventDefault();
    beginTalk();
  }
});
document.addEventListener("keyup", event => { if (event.code === "Space") { event.preventDefault(); endTalk(); } });

sink.addEventListener("change", () => {
  const remote = sink.value === "media_player";
  speaker.disabled = !remote;
  mode.disabled = !remote;
  announce.disabled = !remote;
  progressiveFallback.disabled = !remote;
  routeResult.textContent = "Unsaved route selection.";
});
speaker.addEventListener("change", () => {
  mode.value = speakerModes[speaker.value] || "buffered";
  routeResult.textContent = speakerModes[speaker.value]
    ? `Recommended saved mode: ${mode.value}.`
    : "No tested mode saved for this speaker; buffered is recommended.";
});
saveRoute.addEventListener("click", () => { saveRoute.textContent = "Saving…"; socket.send(JSON.stringify({ type: "route_set", route: selectedRoute() })); });
testRoute.addEventListener("click", () => { testRoute.disabled = true; routeResult.textContent = "Testing…"; socket.send(JSON.stringify({ type: "route_test", route: selectedRoute() })); });
refreshTools.addEventListener("click", () => { refreshTools.disabled = true; refreshTools.textContent = "Refreshing…"; socket.send(JSON.stringify({ type: "tools_refresh" })); });
cancelButton.addEventListener("click", () => { stopPlayback(); socket.send(JSON.stringify({ type: "cancel" })); setPhase("ready", "cancelled"); });
resetConversation.addEventListener("click", () => { stopPlayback(); socket.send(JSON.stringify({ type: "conversation_reset" })); setPhase("resetting"); });
window.addEventListener("online", connect);
window.addEventListener("offline", () => socket?.close());
document.addEventListener("visibilitychange", () => { if (!document.hidden && (!socket || socket.readyState === WebSocket.CLOSED)) connect(); });
window.addEventListener("pagehide", () => {
  clearTimeout(reconnectTimer);
  generation += 1;
  socket?.close();
  microphoneStream?.getTracks().forEach(track => track.stop());
  source?.disconnect();
  capture?.disconnect();
  stopPlayback();
});

connect();
setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "diagnostics_get" })); }, 10000);

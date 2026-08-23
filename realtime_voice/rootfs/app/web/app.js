const statusEl = document.querySelector("#status");
const ptt = document.querySelector("#ptt");
const transcript = document.querySelector("#transcript");
const sink = document.querySelector("#sink");
const speaker = document.querySelector("#speaker");
const mode = document.querySelector("#mode");
const announce = document.querySelector("#announce");
const saveRoute = document.querySelector("#saveRoute");
const refreshTools = document.querySelector("#refreshTools");
const catalogSummary = document.querySelector("#catalogSummary");
const mcpServers = document.querySelector("#mcpServers");

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
let socket;
let context;
let source;
let capture;
let playbackTime = 0;

function websocketUrl() {
  const url = new URL("ws", window.location.href.endsWith("/") ? window.location.href : `${window.location.href}/`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url;
}

function setRoute(route) {
  sink.value = route.sink;
  mode.value = route.mode;
  announce.checked = route.announce;
  if (route.entity_id) speaker.value = route.entity_id;
  const remote = route.sink === "media_player";
  speaker.disabled = !remote;
  mode.disabled = !remote;
  announce.disabled = !remote;
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

async function playPcm(buffer) {
  if (sink.value !== "browser") return;
  context ||= new AudioContext({ sampleRate: 24000 });
  await context.resume();
  const pcm = new Int16Array(buffer);
  const audio = context.createBuffer(1, pcm.length, 24000);
  const channel = audio.getChannelData(0);
  for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
  const node = context.createBufferSource();
  node.buffer = audio;
  node.connect(context.destination);
  playbackTime = Math.max(context.currentTime + 0.04, playbackTime);
  node.start(playbackTime);
  playbackTime += audio.duration;
}

async function startCapture() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    throw new Error("Microphone access requires HTTPS or localhost");
  }
  context ||= new AudioContext();
  await context.resume();
  await context.audioWorklet.addModule("static/pcm-worklet.js");
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  source = context.createMediaStreamSource(stream);
  capture = new AudioWorkletNode(context, "pcm-capture");
  capture.port.onmessage = event => {
    if (socket.readyState === WebSocket.OPEN && ptt.classList.contains("active")) socket.send(event.data);
  };
  source.connect(capture);
}

async function connect() {
  socket = new WebSocket(websocketUrl());
  socket.binaryType = "arraybuffer";
  socket.onopen = () => socket.send(JSON.stringify({ type: "hello", protocol: 1, client_id: clientId, name: navigator.userAgent.slice(0, 60) }));
  socket.onmessage = async event => {
    if (event.data instanceof ArrayBuffer) return playPcm(event.data);
    const message = JSON.parse(event.data);
    if (message.type === "session_ready") {
      statusEl.textContent = "Ready";
      statusEl.classList.add("ready");
      const microphoneAvailable = window.isSecureContext && navigator.mediaDevices?.getUserMedia;
      ptt.disabled = !microphoneAvailable;
      saveRoute.disabled = false;
      refreshTools.disabled = false;
      transcript.textContent = microphoneAvailable
        ? "Hold the button and speak."
        : "Microphone access requires HTTPS (or localhost).";
      setRoute(message.route);
      setMcpStatus(message.mcp);
      socket.send(JSON.stringify({ type: "speakers_list" }));
    } else if (message.type === "speakers") {
      speaker.replaceChildren(...message.items.map(item => new Option(item.name, item.entity_id)));
    } else if (message.type === "route") {
      saveRoute.textContent = "Route saved";
      setTimeout(() => { saveRoute.textContent = "Save output route"; }, 1500);
    } else if (message.type === "mcp_status") {
      setMcpStatus(message.mcp);
      refreshTools.disabled = false;
      refreshTools.textContent = "Refresh tools";
    } else if (message.type === "response.output_audio_transcript.delta") {
      transcript.textContent += message.delta;
    } else if (message.type === "response.done") {
      transcript.textContent += "\n";
    }
  };
  socket.onclose = () => {
    statusEl.textContent = "Disconnected";
    statusEl.classList.remove("ready");
    ptt.disabled = true;
    refreshTools.disabled = true;
    setTimeout(connect, 2000);
  };
}

ptt.addEventListener("pointerdown", async event => {
  event.preventDefault();
  if (!capture) await startCapture();
  playbackTime = context?.currentTime || 0;
  ptt.classList.add("active");
  ptt.textContent = "Listening…";
  transcript.textContent = "";
  socket.send(JSON.stringify({ type: "ptt_start" }));
});

for (const eventName of ["pointerup", "pointercancel", "pointerleave"]) {
  ptt.addEventListener(eventName, () => {
    if (!ptt.classList.contains("active")) return;
    ptt.classList.remove("active");
    ptt.textContent = "Hold to talk";
    socket.send(JSON.stringify({ type: "ptt_stop" }));
  });
}

sink.addEventListener("change", () => setRoute({ sink: sink.value, mode: mode.value, announce: announce.checked, entity_id: speaker.value || null }));
saveRoute.addEventListener("click", () => {
  saveRoute.textContent = "Saving…";
  socket.send(JSON.stringify({
    type: "route_set",
    route: { sink: sink.value, entity_id: sink.value === "media_player" ? speaker.value : null, mode: mode.value, announce: announce.checked, volume: null },
  }));
});

refreshTools.addEventListener("click", () => {
  refreshTools.disabled = true;
  refreshTools.textContent = "Refreshing…";
  socket.send(JSON.stringify({ type: "tools_refresh" }));
});

connect();

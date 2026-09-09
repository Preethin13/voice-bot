if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").catch(() => {});
}

const installBtn = document.getElementById("installBtn");
let installEvent = null;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installEvent = event;
  if (installBtn) installBtn.hidden = false;
});
installBtn?.addEventListener("click", async () => {
  if (!installEvent) return;
  installEvent.prompt();
  await installEvent.userChoice;
  installEvent = null;
  installBtn.hidden = true;
});
const SAMPLE_RATE = 24000;
const logEl = document.getElementById("log");
const orb = document.getElementById("orb");
const statusEl = document.getElementById("status");
const stateLabel = document.getElementById("stateLabel");
const liveChip = document.getElementById("liveChip");
const emptyEl = document.getElementById("empty");
const talkBtn = document.getElementById("talk");
const composer = document.getElementById("composer");
const chatInput = document.getElementById("chatInput");
const voicesEl = document.getElementById("voices");

let selectedVoice = "marin";
let socket = null;
let audioCtx = null;
let processor = null;
let mediaStream = null;
let player = null;
let live = false;
let micArmed = false;
let userLine = null;
let botLine = null;
let speakStartedAt = 0;
let pendingTexts = [];
let sessionWaiters = [];
let connecting = null;

class PcmPlayer {
  constructor(ctx) {
    this.ctx = ctx;
    this.next = 0;
    this.nodes = [];
  }

  get playing() {
    return this.nodes.length > 0;
  }

  interrupt() {
    for (const node of this.nodes) {
      try {
        node.stop();
      } catch {
        /* already stopped */
      }
    }
    this.nodes = [];
    this.next = this.ctx.currentTime;
  }

  pushBase64(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    if (bytes.byteLength < 2) return;
    const aligned = new ArrayBuffer(bytes.byteLength);
    new Uint8Array(aligned).set(bytes);
    const samples = new Int16Array(aligned);
    const f32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) f32[i] = samples[i] / 32768;
    const buffer = this.ctx.createBuffer(1, f32.length, SAMPLE_RATE);
    buffer.getChannelData(0).set(f32);
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.ctx.destination);
    const startAt = Math.max(this.ctx.currentTime + 0.02, this.next);
    src.start(startAt);
    this.next = startAt + buffer.duration;
    this.nodes.push(src);
    src.onended = () => {
      this.nodes = this.nodes.filter((n) => n !== src);
    };
  }
}

function downsample(buffer, inRate, outRate) {
  if (inRate === outRate) return buffer;
  const ratio = inRate / outRate;
  const length = Math.round(buffer.length / ratio);
  const result = new Float32Array(length);
  let offsetResult = 0;
  let offsetBuffer = 0;
  while (offsetResult < result.length) {
    const nextOffset = Math.round((offsetResult + 1) * ratio);
    let accum = 0;
    let count = 0;
    for (let i = offsetBuffer; i < nextOffset && i < buffer.length; i += 1) {
      accum += buffer[i];
      count += 1;
    }
    result[offsetResult] = count ? accum / count : 0;
    offsetResult += 1;
    offsetBuffer = nextOffset;
  }
  return result;
}

function floatToPcm16(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i += 1) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function toBase64(int16) {
  const bytes = new Uint8Array(int16.buffer);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function setState(state, label) {
  orb.dataset.state = state;
  document.body.dataset.state = state;
  stateLabel.textContent = label;
  if (liveChip) {
    liveChip.textContent = live || state !== "idle" ? label : "Idle";
    liveChip.title = live ? "Click to end the session" : "";
    liveChip.style.cursor = live ? "pointer" : "default";
  }
}

function appendLine(role, text, final) {
  if (emptyEl) emptyEl.hidden = true;
  const who = role === "user" ? "You" : "Lumen";
  const initial = role === "user" ? "You" : "L";
  if (role === "user") {
    if (!userLine) {
      userLine = document.createElement("p");
      userLine.className = "line user";
      userLine.innerHTML = `<span class="who">${initial}</span><b></b>`;
      logEl.appendChild(userLine);
    }
    if (text) userLine.querySelector("b").textContent += text;
    if (final) userLine = null;
  } else {
    if (!botLine) {
      botLine = document.createElement("p");
      botLine.className = "line bot";
      botLine.innerHTML = `<span class="who">${initial}</span><b></b>`;
      logEl.appendChild(botLine);
    }
    if (text) botLine.querySelector("b").textContent = (botLine.querySelector("b").textContent || "") + text;
    if (final) botLine = null;
  }
  logEl.scrollTop = logEl.scrollHeight;
}

function renderVoices(voices) {
  voicesEl.innerHTML = "";
  for (const voice of voices) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "voice";
    btn.dataset.gender = voice.gender;
    btn.setAttribute("aria-pressed", String(voice.id === selectedVoice));
    btn.innerHTML = `<span class="avatar">${voice.name[0]}</span><span><em>${voice.gender}</em><strong>${voice.name}</strong><small>${voice.blurb}</small></span>`;
    btn.addEventListener("click", () => {
      if (live) return;
      selectedVoice = voice.id;
      document.body.dataset.voice = voice.id;
      for (const child of voicesEl.children) {
        child.setAttribute("aria-pressed", String(child === btn));
      }
    });
    voicesEl.appendChild(btn);
  }
}

function waitUntilReady() {
  return new Promise((resolve) => {
    if (live && socket && socket.readyState === WebSocket.OPEN) {
      resolve();
      return;
    }
    sessionWaiters.push(resolve);
    if (live && socket && socket.readyState === WebSocket.OPEN) notifyReady();
  });
}

function notifyReady() {
  const waiters = sessionWaiters;
  sessionWaiters = [];
  for (const resolve of waiters) resolve();
}

function flushPendingTexts() {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  while (pendingTexts.length) {
    socket.send(JSON.stringify({ type: "text", text: pendingTexts.shift() }));
  }
}

function markTalkingUi() {
  talkBtn.innerHTML = '<span class="dot"></span><span class="cta-label">End conversation</span>';
  talkBtn.classList.add("live");
  setState("listening", "Listening");
  statusEl.textContent = "Go ahead — speak when you’re ready.";
}

function markConnectedUi() {
  talkBtn.innerHTML = '<span class="dot"></span><span class="cta-label">Start talking</span>';
  talkBtn.classList.remove("live");
  setState("listening", "Connected");
  statusEl.textContent = "Type a message, or tap Start talking to use your mic.";
}

async function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new AudioContext();
    player = new PcmPlayer(audioCtx);
  }
  if (audioCtx.state === "suspended") await audioCtx.resume();
}

function onServerMessage(event) {
  const msg = JSON.parse(event.data);
  if (msg.type === "ready") {
    live = true;
    notifyReady();
    flushPendingTexts();
    if (micArmed) markTalkingUi();
    else markConnectedUi();
  }
  if (msg.type === "status") {
    if (msg.state === "speaking") {
      if (!speakStartedAt) speakStartedAt = Date.now();
      setState("speaking", "Speaking");
    }
    if (msg.state === "listening") {
      speakStartedAt = 0;
      setState("listening", micArmed ? "Listening" : "Connected");
    }
  }
  if (msg.type === "interrupt") {
    if (!player?.playing) return;
    if (speakStartedAt && Date.now() - speakStartedAt < 650) return;
    player.interrupt();
    speakStartedAt = 0;
    setState("listening", "I’m listening");
  }
  if (msg.type === "audio" && msg.audio) {
    if (!speakStartedAt) speakStartedAt = Date.now();
    player.pushBase64(msg.audio);
  }
  if (msg.type === "transcript") appendLine(msg.role, msg.text || "", Boolean(msg.final));
  if (msg.type === "error") {
    statusEl.textContent = msg.message;
    stop();
  }
}

async function connectSession() {
  if (live && socket && socket.readyState === WebSocket.OPEN) return;
  if (connecting) {
    await connecting;
    return;
  }
  connecting = (async () => {
    await ensureAudio();
    setState("listening", "Connecting…");
    statusEl.textContent = "Starting a session…";
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/ws`);
    socket.addEventListener("open", () => {
      socket.send(JSON.stringify({ type: "hello", voice: selectedVoice }));
    });
    socket.addEventListener("message", onServerMessage);
    socket.addEventListener("close", () => {
      connecting = null;
      if (live) stop();
    });
    await waitUntilReady();
    if (!live) throw new Error("Could not start a session.");
  })();
  try {
    await connecting;
  } finally {
    connecting = null;
  }
}

async function armMic() {
  await ensureAudio();
  if (mediaStream) {
    micArmed = true;
    markTalkingUi();
    return;
  }
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  });
  const source = audioCtx.createMediaStreamSource(mediaStream);
  processor = audioCtx.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = (event) => {
    if (!live || !micArmed || !socket || socket.readyState !== WebSocket.OPEN) return;
    const input = event.inputBuffer.getChannelData(0);
    const down = downsample(input, audioCtx.sampleRate, SAMPLE_RATE);
    if (!down.length) return;
    const pcm = floatToPcm16(down);
    socket.send(JSON.stringify({ type: "audio", audio: toBase64(pcm) }));
  };
  const sink = audioCtx.createMediaStreamDestination();
  source.connect(processor);
  processor.connect(sink);
  micArmed = true;
  markTalkingUi();
}

async function startTalking() {
  await connectSession();
  await armMic();
}

async function sendChat(text) {
  appendLine("user", text, true);
  pendingTexts.push(text);
  await connectSession();
  flushPendingTexts();
}

function stop() {
  live = false;
  micArmed = false;
  speakStartedAt = 0;
  notifyReady();
  talkBtn.innerHTML = '<span class="dot"></span><span class="cta-label">Start talking</span>';
  talkBtn.classList.remove("live");
  document.body.dataset.state = "idle";
  setState("idle", "Ready when you are");
  pendingTexts = [];
  sessionWaiters = [];
  connecting = null;
  statusEl.innerHTML =
    '<span class="long">Pick a voice. Type a message or start talking — you can interrupt anytime.</span><span class="short">Type below, or start talking.</span>';
  try {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "stop" }));
      socket.close();
    }
  } catch {
    /* ignore */
  }
  socket = null;
  player?.interrupt();
  try {
    processor?.disconnect();
  } catch {
    /* ignore */
  }
  mediaStream?.getTracks().forEach((track) => track.stop());
  mediaStream = null;
  processor = null;
}

liveChip?.addEventListener("click", () => {
  if (live) stop();
});

talkBtn.addEventListener("click", async () => {
  if (live && micArmed) {
    stop();
    return;
  }
  try {
    await startTalking();
  } catch (err) {
    statusEl.textContent = err.message || "Microphone permission is required.";
    if (!live) stop();
  }
});

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = (chatInput.value || "").trim();
  if (!text) return;
  chatInput.value = "";
  try {
    await sendChat(text);
  } catch (err) {
    statusEl.textContent = err.message || "Could not send that message.";
    if (!live) stop();
  }
});

fetch("/api/voices")
  .then((r) => r.json())
  .then(renderVoices)
  .catch(() => {
    renderVoices([
      { id: "marin", name: "Marin", gender: "female", blurb: "Warm, natural" },
      { id: "coral", name: "Coral", gender: "female", blurb: "Bright" },
      { id: "cedar", name: "Cedar", gender: "male", blurb: "Calm" },
      { id: "ash", name: "Ash", gender: "male", blurb: "Steady" },
    ]);
  });

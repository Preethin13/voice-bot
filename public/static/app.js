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
let peer = null;
let channel = null;
let remoteAudio = null;
let mediaStream = null;
let live = false;
let micArmed = false;
let userLine = null;
let botLine = null;
let speakStartedAt = 0;
let pendingTexts = [];
let sessionWaiters = [];
let connecting = null;
let handlingTools = false;

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
    if (live && channel && channel.readyState === "open") {
      resolve();
      return;
    }
    sessionWaiters.push(resolve);
    if (live && channel && channel.readyState === "open") notifyReady();
  });
}

function notifyReady() {
  const waiters = sessionWaiters;
  sessionWaiters = [];
  for (const resolve of waiters) resolve();
}

function sendEvent(payload) {
  if (!channel || channel.readyState !== "open") return;
  channel.send(JSON.stringify(payload));
}

function flushPendingTexts() {
  if (!channel || channel.readyState !== "open") return;
  while (pendingTexts.length) {
    const text = pendingTexts.shift();
    sendEvent({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text }],
      },
    });
    sendEvent({ type: "response.create" });
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

async function handleFunctionCalls(calls) {
  if (!calls.length || handlingTools) return;
  handlingTools = true;
  try {
    for (const call of calls) {
      const response = await fetch("/api/tool", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: call.name,
          arguments: call.arguments,
        }),
      });
      const data = await response.json();
      sendEvent({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: call.call_id,
          output: data.output || JSON.stringify({ error: "Tool failed." }),
        },
      });
    }
    sendEvent({ type: "response.create" });
  } finally {
    handlingTools = false;
  }
}

function onRealtimeEvent(event) {
  let msg;
  try {
    msg = JSON.parse(event.data);
  } catch {
    return;
  }
  const kind = msg.type || "";
  if (kind === "session.created" || kind === "session.updated") {
    live = true;
    notifyReady();
    flushPendingTexts();
    if (micArmed) markTalkingUi();
    else markConnectedUi();
  }
  if (kind === "input_audio_buffer.speech_started") {
    setState("listening", "I’m listening");
  }
  if (kind === "response.output_audio_transcript.delta" || kind === "response.audio_transcript.delta") {
    if (!speakStartedAt) speakStartedAt = Date.now();
    setState("speaking", "Speaking");
    appendLine("assistant", msg.delta || "", false);
  }
  if (
    kind === "response.output_audio_transcript.done" ||
    kind === "response.audio_transcript.done"
  ) {
    appendLine("assistant", "", true);
    speakStartedAt = 0;
    setState("listening", micArmed ? "Listening" : "Connected");
  }
  if (
    kind === "conversation.item.input_audio_transcription.delta" ||
    kind === "conversation.item.input_audio_transcription.text"
  ) {
    const text = msg.delta || msg.transcript || "";
    if (text) appendLine("user", text, false);
  }
  if (kind === "conversation.item.input_audio_transcription.completed") {
    const text = (msg.transcript || "").trim();
    if (text) appendLine("user", text, true);
  }
  if (kind === "response.done") {
    const calls = (msg.response?.output || []).filter((item) => item.type === "function_call");
    if (calls.length) handleFunctionCalls(calls);
  }
  if (kind === "error") {
    const message = msg.error?.message || msg.message || "Session error";
    statusEl.textContent = message;
    stop();
  }
}

async function connectSession({ withMic }) {
  if (live && channel && channel.readyState === "open") {
    if (withMic) await armMic();
    return;
  }
  if (connecting) {
    await connecting;
    if (withMic) await armMic();
    return;
  }
  connecting = (async () => {
    setState("listening", "Connecting…");
    statusEl.textContent = "Starting a session…";
    const tokenRes = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice: selectedVoice }),
    });
    const tokenData = await tokenRes.json();
    if (!tokenRes.ok || !tokenData.value) {
      throw new Error(tokenData.error || "Could not start a session.");
    }

    peer = new RTCPeerConnection();
    remoteAudio = document.createElement("audio");
    remoteAudio.autoplay = true;
    peer.ontrack = (ev) => {
      remoteAudio.srcObject = ev.streams[0];
    };

    if (withMic) {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
      mediaStream.getTracks().forEach((track) => peer.addTrack(track, mediaStream));
      micArmed = true;
    } else {
      peer.addTransceiver("audio", { direction: "recvonly" });
      micArmed = false;
    }

    channel = peer.createDataChannel("oai-events");
    channel.addEventListener("message", onRealtimeEvent);
    channel.addEventListener("close", () => {
      if (live) stop();
    });

    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    const model = tokenData.model || "gpt-realtime-2.1";
    const sdpRes = await fetch(`https://api.openai.com/v1/realtime/calls?model=${encodeURIComponent(model)}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${tokenData.value}`,
        "Content-Type": "application/sdp",
      },
      body: offer.sdp,
    });
    if (!sdpRes.ok) {
      throw new Error("Could not connect the voice session.");
    }
    const answer = await sdpRes.text();
    await peer.setRemoteDescription({ type: "answer", sdp: answer });
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
  if (mediaStream) {
    micArmed = true;
    markTalkingUi();
    return;
  }
  if (live) stop();
  await connectSession({ withMic: true });
}

async function startTalking() {
  await connectSession({ withMic: true });
}

async function sendChat(text) {
  appendLine("user", text, true);
  pendingTexts.push(text);
  await connectSession({ withMic: false });
  flushPendingTexts();
}

function stop() {
  live = false;
  micArmed = false;
  speakStartedAt = 0;
  handlingTools = false;
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
    channel?.close();
  } catch {
    /* ignore */
  }
  try {
    peer?.close();
  } catch {
    /* ignore */
  }
  mediaStream?.getTracks().forEach((track) => track.stop());
  if (remoteAudio) {
    remoteAudio.srcObject = null;
    remoteAudio = null;
  }
  mediaStream = null;
  peer = null;
  channel = null;
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

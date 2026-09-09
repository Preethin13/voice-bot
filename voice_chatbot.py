#!/usr/bin/env python3
"""Real-time voice chatbot using the OpenAI Realtime API over WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import os
import signal
import ssl
import sys
import threading
import time
from typing import Any

import certifi
import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from realtime_config import DEFAULT_MODEL, DEFAULT_VOICE, SAMPLE_RATE, WS_URL, session_update

CHANNELS = 1
CHUNK_MS = 40
CHUNK_FRAMES = SAMPLE_RATE * CHUNK_MS // 1000
BYTES_PER_SAMPLE = 2
PREROLL_MS = 120
PREROLL_TIMEOUT_S = 0.08
ECHO_HOLD_S = 0.45
BARGE_IN_RMS = 0.055


class AudioPlayer:
    """PCM16 playback with preroll (avoids gaps) and echo-aware barge-in."""

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._pcm = bytearray()
        self._lock = threading.Lock()
        self.current_item_id: str | None = None
        self.played_samples = 0
        self.playback_rms = 0.0
        self._last_play_mono = 0.0
        self._waiting_preroll = True
        self._preroll_started: float | None = None
        self._preroll_bytes = int(sample_rate * PREROLL_MS / 1000) * BYTES_PER_SAMPLE
        self._stream: sd.OutputStream | None = None

    def start(self) -> None:
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="int16",
            callback=self._callback,
            blocksize=CHUNK_FRAMES,
            latency="high",
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _callback(self, outdata, frames, _time, _status) -> None:  # type: ignore[no-untyped-def]
        needed_bytes = frames * BYTES_PER_SAMPLE
        now = time.monotonic()
        with self._lock:
            waiting = self._waiting_preroll and len(self._pcm) < self._preroll_bytes
            if waiting and self._preroll_started is not None:
                if now - self._preroll_started >= PREROLL_TIMEOUT_S and self._pcm:
                    waiting = False
            if waiting:
                raw = b""
            else:
                self._waiting_preroll = False
                self._preroll_started = None
                take = min(needed_bytes, len(self._pcm))
                raw = bytes(self._pcm[:take])
                del self._pcm[:take]
                self.played_samples += take // BYTES_PER_SAMPLE
                if take:
                    self._last_play_mono = now
        if raw:
            samples = np.frombuffer(raw, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768.0
            self.playback_rms = rms
            if len(samples) < frames:
                samples = np.concatenate(
                    [samples, np.zeros(frames - len(samples), dtype=np.int16)]
                )
            outdata[:] = samples.reshape(-1, 1)
        else:
            self.playback_rms *= 0.6
            outdata.fill(0)

    def enqueue(self, item_id: str | None, pcm16: bytes) -> None:
        with self._lock:
            if item_id and item_id != self.current_item_id:
                if self.current_item_id not in (None, "assistant"):
                    self._pcm.clear()
                    self.played_samples = 0
                    self._waiting_preroll = True
                    self._preroll_started = None
                self.current_item_id = item_id
            elif self.current_item_id is None:
                self.current_item_id = item_id or "assistant"
            self._pcm.extend(pcm16)
            if self._waiting_preroll and self._preroll_started is None and self._pcm:
                self._preroll_started = time.monotonic()

    def flush_preroll(self) -> None:
        with self._lock:
            self._waiting_preroll = False
            self._preroll_started = None

    def interrupt(self) -> tuple[str | None, int]:
        with self._lock:
            item_id = self.current_item_id
            played_ms = int(self.played_samples / self.sample_rate * 1000)
            self._pcm.clear()
            self.current_item_id = None
            self.played_samples = 0
            self._waiting_preroll = True
            self._preroll_started = None
            self.playback_rms = 0.0
        return item_id, played_ms

    def hold_echo_gate(self) -> None:
        if self._last_play_mono > 0:
            self._last_play_mono = time.monotonic()

    def is_playing(self) -> bool:
        if self._last_play_mono <= 0:
            return False
        recently = (time.monotonic() - self._last_play_mono) < ECHO_HOLD_S
        return recently or self.playback_rms > 0.01

    def should_ignore_barge_in(self, mic_rms: float = 0.0) -> bool:
        if not self.is_playing():
            return False
        gate = max(BARGE_IN_RMS, self.playback_rms * 2.4 + 0.02)
        return mic_rms < gate


class VoiceChatbot:
    def __init__(self, api_key: str, model: str, voice: str) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.player = AudioPlayer()
        self.send_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._input_stream: sd.InputStream | None = None
        self._closing = False
        self._assistant_line_open = False
        self._user_line_open = False
        self._mic_rms = 0.0
        self._cleared_for_item: str | None = None

    def _mic_callback(self, indata, _frames, _time, status) -> None:  # type: ignore[no-untyped-def]
        if status:
            print(f"[mic] {status}", file=sys.stderr)
        pcm = indata.tobytes()
        if indata.ndim == 2 and indata.shape[1] != 1:
            pcm = audioop.tomono(pcm, BYTES_PER_SAMPLE, 0.5, 0.5)
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples ** 2))) / 32768.0 if samples.size else 0.0
        self._mic_rms = (self._mic_rms * 0.7) + (rms * 0.3)
        # Drop speaker echo so the API does not treat playback as a new user turn.
        if self.player.should_ignore_barge_in(self._mic_rms):
            return
        event = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.send_queue.put_nowait, event)

    def start_mic(self) -> None:
        self._input_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_FRAMES,
            callback=self._mic_callback,
        )
        self._input_stream.start()

    def stop_mic(self) -> None:
        if self._input_stream is not None:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None

    def session_update(self) -> dict[str, Any]:
        return session_update(model=self.model, voice=self.voice, interrupt=True)

    async def sender(self, ws: Any) -> None:
        try:
            while True:
                event = await self.send_queue.get()
                if event is None:
                    return
                await ws.send(json.dumps(event))
        except ConnectionClosed:
            return

    async def handle_server_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type", "")

        if kind == "error":
            err = event.get("error") or event
            print(f"\n[api error] {err.get('message', err)}", file=sys.stderr)
            return

        if kind == "session.created":
            print("Connected. Speak whenever you are ready. Ctrl+C to quit.\n", flush=True)
            return

        if kind == "input_audio_buffer.speech_started":
            if self.player.should_ignore_barge_in(self._mic_rms):
                return
            self.player.interrupt()
            self._assistant_line_open = False
            self._user_line_open = True
            sys.stdout.write("\nYou: ")
            sys.stdout.flush()
            return

        if kind in {
            "conversation.item.input_audio_transcription.delta",
            "conversation.item.input_audio_transcription.text",
        }:
            sys.stdout.write(event.get("delta") or event.get("transcript") or "")
            sys.stdout.flush()
            return

        if kind == "conversation.item.input_audio_transcription.completed":
            if self._user_line_open:
                print()
                self._user_line_open = False
            else:
                transcript = (event.get("transcript") or "").strip()
                if transcript:
                    print(f"You: {transcript}")
            return

        if kind in {
            "response.output_audio_transcript.delta",
            "response.audio_transcript.delta",
        }:
            delta = event.get("delta") or ""
            if delta:
                if not self._assistant_line_open:
                    sys.stdout.write("Assistant: ")
                    self._assistant_line_open = True
                sys.stdout.write(delta)
                sys.stdout.flush()
            return

        if kind in {"response.created"}:
            self._cleared_for_item = None
            return

        if kind in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            if self._assistant_line_open:
                print()
                self._assistant_line_open = False
            return

        if kind == "response.done":
            if self._assistant_line_open:
                print()
                self._assistant_line_open = False
            self.player.flush_preroll()
            return

        if kind in {"response.output_audio.delta", "response.audio.delta"}:
            audio_b64 = event.get("delta")
            if not audio_b64:
                return
            item_id = event.get("item_id")
            if item_id and item_id != self._cleared_for_item:
                self._cleared_for_item = item_id
                await self.send_queue.put({"type": "input_audio_buffer.clear"})
            self.player.enqueue(item_id, base64.b64decode(audio_b64))
            return

    async def receiver(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self.handle_server_event(event)
        except ConnectionClosed as exc:
            if not self._closing:
                print(f"\nConnection closed: {exc}", file=sys.stderr)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        url = f"{WS_URL}?model={self.model}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        print(f"Opening Realtime session ({self.model}, voice={self.voice})...", flush=True)
        try:
            self.player.start()
            self.start_mic()
        except Exception as exc:  # noqa: BLE001
            print(
                "Could not open the microphone or speakers. On macOS, grant "
                "microphone access to Terminal/Cursor and install PortAudio "
                f"(brew install portaudio).\nDetails: {exc}",
                file=sys.stderr,
            )
            raise

        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            async with ws_connect(
                url,
                additional_headers=headers,
                max_size=16 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
                ssl=ssl_context,
            ) as ws:
                await self.send_queue.put(self.session_update())
                sender_task = asyncio.create_task(self.sender(ws), name="ws-sender")
                try:
                    await self.receiver(ws)
                finally:
                    self._closing = True
                    await self.send_queue.put(None)
                    await sender_task
        finally:
            self.stop_mic()
            self.player.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talk to a real-time AI voice chatbot.")
    parser.add_argument(
        "--provider",
        choices=("auto", "openai", "claude"),
        default="auto",
        help="auto uses Claude if ANTHROPIC_API_KEY is set, otherwise OpenAI Realtime",
    )
    parser.add_argument("--model", default=None, help="Provider-specific model id")
    parser.add_argument(
        "--voice",
        default=os.getenv("VOICE", DEFAULT_VOICE),
        choices=("marin", "coral", "cedar", "ash"),
    )
    return parser.parse_args()


def _resolve_provider(requested: str) -> str:
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if requested != "auto":
        return requested
    if anthropic_key:
        return "claude"
    if openai_key:
        return "openai"
    return "openai"


def main() -> int:
    load_dotenv()
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    args = parse_args()
    provider = _resolve_provider(args.provider)

    if provider == "claude":
        from claude_voice import ClaudeVoiceChatbot, DEFAULT_MODEL as CLAUDE_MODEL

        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            print(
                "Set ANTHROPIC_API_KEY in .env (https://console.anthropic.com/settings/keys).",
                file=sys.stderr,
            )
            return 1
        model = args.model or os.getenv("ANTHROPIC_MODEL", CLAUDE_MODEL)
        bot: Any = ClaudeVoiceChatbot(api_key=api_key, model=model)
    else:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key or api_key in {"sk-...", "sk-"}:
            print(
                "Set OPENAI_API_KEY in a .env file (see .env.example) or use "
                "--provider claude with ANTHROPIC_API_KEY.",
                file=sys.stderr,
            )
            return 1
        model = args.model or os.getenv("OPENAI_REALTIME_MODEL", DEFAULT_MODEL)
        bot = VoiceChatbot(api_key=api_key, model=model, voice=args.voice)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(*_args: object) -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    try:
        loop.run_until_complete(bot.run())
    except (asyncio.CancelledError, KeyboardInterrupt):
        print("\nBye.")
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

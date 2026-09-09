#!/usr/bin/env python3
"""Voice chatbot: local speech-to-text, Claude for replies, macOS speech for audio."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from typing import Any

import certifi
import httpx
import numpy as np
import sounddevice as sd
from anthropic import AsyncAnthropic
from faster_whisper import WhisperModel

SAMPLE_RATE = 16_000
CHANNELS = 1
CHUNK_MS = 30
CHUNK_FRAMES = SAMPLE_RATE * CHUNK_MS // 1000
SPEECH_RMS = 0.012
MIN_SPEECH_CHUNKS = 8
SILENCE_CHUNKS = 18
MAX_SECONDS = 20
DEFAULT_MODEL = "claude-haiku-4-5"
WHISPER_MODEL = "tiny.en"

INSTRUCTIONS = (
    "You are talking with someone in a live voice chat. "
    "Sound like a real person: warm, clear, and conversational. "
    "Answer what they just said. Never greet with 'need a hand with anything else' "
    "or similar customer-service closers. "
    "If they say hi, greet briefly and wait. If they ask a question, answer in "
    "one or two natural sentences, then stop. Do not thank them every turn."
)

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _mac_voice() -> str:
    return os.getenv("MAC_VOICE", "Samantha")


class ClaudeVoiceChatbot:
    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self.history: list[dict[str, str]] = []
        http = httpx.AsyncClient(verify=certifi.where(), timeout=60.0)
        self.client = AsyncAnthropic(api_key=api_key, http_client=http)
        self._whisper: WhisperModel | None = None
        self._say_proc: subprocess.Popen[bytes] | None = None

    def load_whisper(self) -> None:
        print("Loading local speech-to-text (first run downloads a small Whisper model)...")
        self._whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")

    def _transcribe(self, audio: np.ndarray) -> str:
        assert self._whisper is not None
        segments, _info = self._whisper.transcribe(
            audio,
            language="en",
            vad_filter=False,
            beam_size=1,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    def record_utterance(self) -> np.ndarray | None:
        chunks: list[np.ndarray] = []
        speech_seen = 0
        silence = 0
        max_chunks = int(MAX_SECONDS * 1000 / CHUNK_MS)
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=CHUNK_FRAMES,
        ) as stream:
            for _ in range(max_chunks):
                data, overflowed = stream.read(CHUNK_FRAMES)
                if overflowed:
                    print("[mic overflow]", file=sys.stderr)
                frame = data.reshape(-1)
                chunks.append(frame.copy())
                rms = float(np.sqrt(np.mean(np.square(frame))))
                if rms >= SPEECH_RMS:
                    speech_seen += 1
                    silence = 0
                elif speech_seen >= MIN_SPEECH_CHUNKS:
                    silence += 1
                    if silence >= SILENCE_CHUNKS:
                        break
        if speech_seen < MIN_SPEECH_CHUNKS:
            return None
        audio = np.concatenate(chunks)
        # Trim trailing silence roughly.
        return audio

    def stop_speaking(self) -> None:
        if self._say_proc is not None and self._say_proc.poll() is None:
            self._say_proc.terminate()
            try:
                self._say_proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._say_proc.kill()
        self._say_proc = None

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        say = shutil.which("say")
        if not say:
            print(f"(no macOS say command) {text}")
            return
        self._say_proc = subprocess.Popen(
            [say, "-v", _mac_voice(), text],
        )
        self._say_proc.wait()
        self._say_proc = None

    async def reply(self, user_text: str) -> str:
        self.history.append({"role": "user", "content": user_text})
        # Keep a short rolling window so prompts stay cheap.
        messages: list[dict[str, Any]] = self.history[-16:]
        spoken = ""
        buffer = ""
        print("Assistant: ", end="", flush=True)
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=300,
            system=INSTRUCTIONS,
            messages=messages,
        ) as stream:
            async for piece in stream.text_stream:
                print(piece, end="", flush=True)
                buffer += piece
                parts = SENTENCE_SPLIT.split(buffer)
                if len(parts) > 1:
                    for sentence in parts[:-1]:
                        await asyncio.to_thread(self.speak, sentence)
                        spoken += sentence + " "
                    buffer = parts[-1]
            final = await stream.get_final_message()
        leftover = buffer.strip()
        if leftover:
            await asyncio.to_thread(self.speak, leftover)
            spoken += leftover
        print()
        assistant_text = ""
        for block in final.content:
            if getattr(block, "type", None) == "text":
                assistant_text += block.text
        assistant_text = assistant_text.strip() or spoken.strip()
        self.history.append({"role": "assistant", "content": assistant_text})
        return assistant_text

    async def run(self) -> None:
        await asyncio.to_thread(self.load_whisper)
        print(
            f"Claude voice loop ready ({self.model}). Speak, then pause. "
            "This is turn-based (not OpenAI-style speech-to-speech). Ctrl+C to quit.\n"
        )
        while True:
            print("Listening...")
            audio = await asyncio.to_thread(self.record_utterance)
            if audio is None:
                continue
            print("Transcribing...")
            text = await asyncio.to_thread(self._transcribe, audio)
            if not text:
                print("(didn't catch that)")
                continue
            print(f"You: {text}")
            try:
                await self.reply(user_text=text)
            except Exception as exc:  # noqa: BLE001
                print(f"\n[claude error] {exc}", file=sys.stderr)
                if self.history and self.history[-1]["role"] == "user":
                    self.history.pop()

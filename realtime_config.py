"""Shared OpenAI Realtime session settings."""

from __future__ import annotations

from typing import Any

from knowledge import ALL_TOOLS

SAMPLE_RATE = 24_000
DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
WS_URL = "wss://api.openai.com/v1/realtime"

VOICES: list[dict[str, str]] = [
    {
        "id": "marin",
        "name": "Marin",
        "gender": "female",
        "blurb": "Warm, natural, easy to follow",
    },
    {
        "id": "coral",
        "name": "Coral",
        "gender": "female",
        "blurb": "Bright and articulate",
    },
    {
        "id": "cedar",
        "name": "Cedar",
        "gender": "male",
        "blurb": "Calm and grounded",
    },
    {
        "id": "ash",
        "name": "Ash",
        "gender": "male",
        "blurb": "Steady and clear",
    },
]

VOICE_IDS = {v["id"] for v in VOICES}

INSTRUCTIONS = (
    "You are Lumen, a highly capable live companion. You have tools that look up "
    "current weather and live world knowledge. Use them. Never claim you lack internet, "
    "a knowledge cutoff, or that you cannot look something up. "
    "Sound like a real person: warm, clear, and conversational. "
    "Answer what they just said. Do not use a customer-service closer. "
    "Never start a turn with phrases like 'need a hand with anything else', "
    "'anything else I can help with', or 'want me to look something else up'. "
    "Those lines are not greetings. "
    "If they say hi, greet them briefly and wait. Do not call tools for greetings. "
    "If they ask a question, give a precise answer in one to three natural sentences, then stop. "
    "Do not thank them every time. Do not ask if they need anything else on every reply. "
    "If they interrupt you, drop what you were saying and respond to the new utterance. "
    "If the audio is noise or an echo of yourself, stay silent. "
    "When they ask about weather, temperature, or conditions outside, call get_current_weather "
    "right away. Do not ask where they are. Leave place empty unless they named a city. "
    "For news, sports, science, people, history, prices, scores, how things work, or any fact "
    "you are not certain about, call lookup_world with a clear query. Then speak the answer "
    "from the tool, not from memory. If the tool errors, say you could not find it — do not invent."
)


def session_update(
    *,
    model: str,
    voice: str,
    interrupt: bool = True,
    speed: float = 0.96,
    include_tools: bool = False,
) -> dict[str, Any]:
    if voice not in VOICE_IDS:
        voice = DEFAULT_VOICE
    session: dict[str, Any] = {
            "type": "realtime",
            "model": model,
            "output_modalities": ["audio"],
            "instructions": INSTRUCTIONS,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "noise_reduction": {"type": "near_field"},
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "medium",
                        "interrupt_response": True,
                        "create_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "voice": voice,
                    "speed": speed,
                },
            },
    }
    if include_tools:
        session["tools"] = ALL_TOOLS
        session["tool_choice"] = "auto"
    return {"type": "session.update", "session": session}

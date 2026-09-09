"""Live world lookup for Lumen (web search + encyclopedic fallback)."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx

from weather import WEATHER_TOOL, get_current_weather

LOOKUP_TOOL = {
    "type": "function",
    "name": "lookup_world",
    "description": (
        "Look up live facts from the web: news, sports, science, people, history, "
        "prices, scores, how things work, current events, and anything you are not sure about. "
        "Call this instead of guessing or saying you do not know. Do not use this for greetings."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The question or topic to research, with any names, dates, or places.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

ALL_TOOLS = [WEATHER_TOOL, LOOKUP_TOOL]

_LOOKUP_MODELS = (
    os.getenv("OPENAI_LOOKUP_MODEL", "gpt-4.1-mini"),
    "gpt-4o-mini",
    "gpt-4o",
)


def _output_text(data: dict[str, Any]) -> str:
    text = (data.get("output_text") or "").strip()
    if text:
        return text
    chunks: list[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") in {"output_text", "text"}:
                chunks.append(part.get("text") or "")
    return "\n".join(chunks).strip()


async def _openai_web_search(query: str, api_key: str) -> str | None:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    prompt = (
        "Research this with live web search. Return accurate current facts only. "
        "Include key names, numbers, dates, and places. Under 150 words. "
        "If sources disagree, say so briefly.\n\n"
        f"{query}"
    )
    async with httpx.AsyncClient(timeout=40.0) as client:
        seen: set[str] = set()
        for model in _LOOKUP_MODELS:
            if model in seen:
                continue
            seen.add(model)
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json={
                    "model": model,
                    "tools": [{"type": "web_search"}],
                    "input": prompt,
                },
            )
            if response.status_code >= 400:
                continue
            text = _output_text(response.json())
            if text:
                return text
    return None


async def _wikipedia(query: str) -> str | None:
    headers = {"User-Agent": "Lumen/1.0 (voice companion; research fallback)"}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        search = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "opensearch",
                "search": query,
                "limit": 3,
                "namespace": 0,
                "format": "json",
            },
        )
        search.raise_for_status()
        payload = search.json()
        titles = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        if not titles:
            return None
        summaries: list[str] = []
        for title in titles[:2]:
            page = await client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(str(title), safe='')}"
            )
            if page.status_code >= 400:
                continue
            data = page.json()
            extract = (data.get("extract") or "").strip()
            if extract:
                summaries.append(f"{data.get('title')}: {extract}")
        return "\n".join(summaries)[:3500] if summaries else None


async def lookup_world(*, query: str, api_key: str) -> str:
    topic = (query or "").strip()
    if not topic:
        return json.dumps({"error": "Empty lookup query."})
    try:
        web = await _openai_web_search(topic, api_key)
        if web:
            return json.dumps({"answer": web[:4000], "source": "live_web"})
        wiki = await _wikipedia(topic)
        if wiki:
            return json.dumps({"answer": wiki, "source": "wikipedia"})
        return json.dumps({"error": "No live results found."})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Lookup failed: {exc}"})


async def run_tool(
    name: str,
    args: dict[str, Any],
    *,
    ip: str | None,
    api_key: str,
) -> str:
    if name == "get_current_weather":
        return await get_current_weather(place=args.get("place"), ip=ip)
    if name == "lookup_world":
        return await lookup_world(query=str(args.get("query") or ""), api_key=api_key)
    return json.dumps({"error": f"Unknown tool {name}"})

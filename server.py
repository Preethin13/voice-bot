#!/usr/bin/env python3
"""Web UI for the OpenAI Realtime voice chatbot."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from pathlib import Path
from typing import Any

import certifi
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from knowledge import run_tool
from realtime_config import DEFAULT_MODEL, VOICE_IDS, VOICES, WS_URL, session_update
from weather import client_ip

load_dotenv()

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"

app = FastAPI(title="Voice chatbot")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/voices")
async def voices() -> list[dict[str, str]]:
    return VOICES


@app.post("/api/session")
async def create_ephemeral_session(request: Request) -> JSONResponse:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key in {"sk-...", "sk-"}:
        return JSONResponse(
            {"error": "Set OPENAI_API_KEY in the project .env file."}, status_code=500
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    voice = (body or {}).get("voice") or "marin"
    if voice not in VOICE_IDS:
        voice = "marin"
    model = os.getenv("OPENAI_REALTIME_MODEL", DEFAULT_MODEL)
    session = session_update(model=model, voice=voice, include_tools=True)["session"]
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"session": session},
        )
    data = response.json()
    if response.status_code >= 400:
        err = data.get("error") or data
        message = err.get("message") if isinstance(err, dict) else str(err)
        return JSONResponse({"error": message or "Could not start a session."}, status_code=502)
    value = data.get("value") or (data.get("client_secret") or {}).get("value")
    if not value:
        return JSONResponse({"error": "OpenAI did not return a session key."}, status_code=502)
    return JSONResponse({"value": value, "model": model})


@app.post("/api/tool")
async def run_client_tool(request: Request) -> JSONResponse:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body or {}).get("name") or ""
    args = (body or {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    ip = client_ip(request.headers, request.client.host if request.client else None)
    output = await run_tool(name, args if isinstance(args, dict) else {}, ip=ip, api_key=api_key)
    return JSONResponse({"output": output})


async def openai_session(
    api_key: str, model: str, voice: str, client: WebSocket, detected_ip: str | None
) -> None:
    url = f"{WS_URL}?model={model}"
    headers = {"Authorization": f"Bearer {api_key}"}
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    outbound: asyncio.Queue[str | None] = asyncio.Queue()
    ignore_barge_until = 0.0
    cleared_for_item: str | None = None

    async def pump_client() -> None:
        try:
            while True:
                raw = await client.receive_text()
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "audio" and msg.get("audio"):
                    await outbound.put(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": msg["audio"],
                            }
                        )
                    )
                elif kind == "text":
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    await outbound.put(
                        json.dumps(
                            {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": text}],
                                },
                            }
                        )
                    )
                    await outbound.put(json.dumps({"type": "response.create"}))
                elif kind == "stop":
                    await outbound.put(None)
                    return
        except WebSocketDisconnect:
            await outbound.put(None)

    async def pump_openai(openai_ws: Any) -> None:
        nonlocal ignore_barge_until, cleared_for_item
        try:
            async for raw in openai_ws:
                event = json.loads(raw)
                kind = event.get("type", "")
                if kind == "error":
                    err = event.get("error") or event
                    await client.send_json(
                        {"type": "error", "message": err.get("message", str(err))}
                    )
                elif kind == "input_audio_buffer.speech_started":
                    if time.monotonic() < ignore_barge_until:
                        continue
                    await client.send_json({"type": "interrupt"})
                elif kind in {
                    "conversation.item.input_audio_transcription.delta",
                    "conversation.item.input_audio_transcription.text",
                }:
                    text = event.get("delta") or event.get("transcript") or ""
                    if text:
                        await client.send_json(
                            {"type": "transcript", "role": "user", "text": text, "final": False}
                        )
                elif kind == "conversation.item.input_audio_transcription.completed":
                    text = (event.get("transcript") or "").strip()
                    if text:
                        await client.send_json(
                            {"type": "transcript", "role": "user", "text": text, "final": True}
                        )
                elif kind in {
                    "response.output_audio_transcript.delta",
                    "response.audio_transcript.delta",
                    "response.output_text.delta",
                    "response.text.delta",
                }:
                    delta = event.get("delta") or ""
                    if delta:
                        await client.send_json(
                            {
                                "type": "transcript",
                                "role": "assistant",
                                "text": delta,
                                "final": False,
                            }
                        )
                        await client.send_json({"type": "status", "state": "speaking"})
                elif kind in {
                    "response.output_audio_transcript.done",
                    "response.audio_transcript.done",
                }:
                    await client.send_json(
                        {"type": "transcript", "role": "assistant", "text": "", "final": True}
                    )
                    await client.send_json({"type": "status", "state": "listening"})
                elif kind == "response.done":
                    response = event.get("response") or {}
                    calls = [
                        item
                        for item in (response.get("output") or [])
                        if item.get("type") == "function_call"
                    ]
                    if not calls:
                        continue
                    for call in calls:
                        name = call.get("name")
                        call_id = call.get("call_id")
                        try:
                            args = json.loads(call.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        if name:
                            output = await run_tool(
                                name,
                                args if isinstance(args, dict) else {},
                                ip=detected_ip,
                                api_key=api_key,
                            )
                        else:
                            output = json.dumps({"error": "Missing tool name"})
                        await outbound.put(
                            json.dumps(
                                {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": call_id,
                                        "output": output,
                                    },
                                }
                            )
                        )
                    await outbound.put(json.dumps({"type": "response.create"}))
                elif kind in {"response.output_audio.delta", "response.audio.delta"}:
                    audio = event.get("delta")
                    if not audio:
                        continue
                    item_id = event.get("item_id")
                    if item_id and item_id != cleared_for_item:
                        cleared_for_item = item_id
                        ignore_barge_until = time.monotonic() + 0.7
                        await outbound.put(json.dumps({"type": "input_audio_buffer.clear"}))
                    await client.send_json({"type": "audio", "audio": audio})
                    await client.send_json({"type": "status", "state": "speaking"})
                elif kind == "response.created":
                    cleared_for_item = None
        except (ConnectionClosed, WebSocketDisconnect):
            return

    async def pump_out(openai_ws: Any) -> None:
        try:
            while True:
                payload = await outbound.get()
                if payload is None:
                    return
                await openai_ws.send(payload)
        except ConnectionClosed:
            return

    async with ws_connect(
        url,
        additional_headers=headers,
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
        ssl=ssl_context,
    ) as openai_ws:
        while True:
            event = json.loads(await openai_ws.recv())
            if event.get("type") == "error":
                err = event.get("error") or event
                await client.send_json(
                    {"type": "error", "message": err.get("message", str(err))}
                )
                return
            if event.get("type") == "session.created":
                break
        await openai_ws.send(
            json.dumps(session_update(model=model, voice=voice, interrupt=True, include_tools=True))
        )
        try:
            while True:
                event = json.loads(
                    await asyncio.wait_for(openai_ws.recv(), timeout=3.0)
                )
                if event.get("type") == "error":
                    err = event.get("error") or event
                    await client.send_json(
                        {"type": "error", "message": err.get("message", str(err))}
                    )
                    return
                if event.get("type") == "session.updated":
                    break
        except asyncio.TimeoutError:
            pass
        client_task = asyncio.create_task(pump_client())
        out_task = asyncio.create_task(pump_out(openai_ws))
        await client.send_json({"type": "ready"})
        in_task = asyncio.create_task(pump_openai(openai_ws))
        _done, pending = await asyncio.wait(
            {client_task, out_task, in_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        await outbound.put(None)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@app.websocket("/ws")
async def voice_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key in {"sk-...", "sk-"}:
        await websocket.send_json(
            {"type": "error", "message": "Set OPENAI_API_KEY in the project .env file."}
        )
        await websocket.close()
        return

    try:
        hello = json.loads(await websocket.receive_text())
    except Exception:
        await websocket.close()
        return

    voice = hello.get("voice") or "marin"
    if voice not in VOICE_IDS:
        voice = "marin"
    model = os.getenv("OPENAI_REALTIME_MODEL", DEFAULT_MODEL)

    detected_ip = client_ip(
        websocket.headers, websocket.client.host if websocket.client else None
    )
    try:
        await openai_session(api_key, model, voice, websocket, detected_ip)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass


def main() -> None:
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )


if __name__ == "__main__":
    main()

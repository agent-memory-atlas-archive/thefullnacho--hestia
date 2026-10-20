"""Goals page API: list, approve or discard a draft, tick or untick a step. No model.

Writes follow the recipe review guard: a custom header, a same-origin check and a JSON body,
so a link or a form on another page cannot approve a plan or tick a step. The brain's network
boundary is still the access control; this only stops cross-site requests riding on it.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import goals_store as store

HEADER = "X-Hestia-Goals"
MAX_BODY = 64 * 1024


async def body_json(request: Request) -> dict:
    if request.headers.get(HEADER) != "1":
        raise ValueError("Use the Goals page for this action.")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise ValueError("Goal changes must come from this Hestia page.")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise ValueError("Expected JSON from the Goals page.")
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ValueError("Request too large.")
    data = json.loads(raw or b"{}")
    if not isinstance(data, dict):
        raise ValueError("Expected an object.")
    return data


def router() -> APIRouter:
    api = APIRouter()

    async def run(fn, *args, **kwargs):
        try:
            return {"ok": True, "goal": await asyncio.to_thread(fn, *args, **kwargs)}
        except LookupError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
        except (ValueError, TypeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)

    @api.get("/api/goals")
    async def list_goals():
        live = await asyncio.to_thread(store.goals, ("proposed", "active"))
        done = await asyncio.to_thread(store.goals, ("done",), 10)
        return JSONResponse({"goals": live, "done": done}, headers={"Cache-Control": "no-store"})

    @api.post("/api/goals/{gid}/approve")
    async def approve(gid: int, request: Request):
        try:
            data = await body_json(request)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        return await run(store.approve, gid, data.get("title"), data.get("steps"),
                         why=data.get("why") or "", target=data.get("target"))

    @api.post("/api/goals/{gid}/discard")
    async def discard(gid: int, request: Request):
        try:
            await body_json(request)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        return await run(store.discard, gid)

    @api.post("/api/goals/steps/{sid}/{action}")
    async def step(sid: int, action: str, request: Request):
        try:
            await body_json(request)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        fn = {"done": store.mark_done, "undo": store.undo_done}.get(action)
        if fn is None:
            return JSONResponse({"ok": False, "error": f"Unknown step action '{action}'."}, status_code=404)
        try:
            result = await asyncio.to_thread(fn, sid)
            goal = await asyncio.to_thread(store.get, result["step"]["goal_id"])
        except LookupError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        return {"ok": True, "goal": goal, "already": result["already"]}

    return api

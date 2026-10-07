"""HTTP API for the ground imaging station compact-storage service."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import CompactRequest, DrillIn
from .store import Rejected, SimulatedPowerLoss, Store

DATA_DIR = os.environ.get("DATA_DIR")
if not DATA_DIR:
    # Local (non-Compose) runs: keep durable state inside the workspace.
    DATA_DIR = str(Path(__file__).resolve().parent.parent / ".runtime-data")
CRASH_MODE = os.environ.get("CRASH_MODE", "soft")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="地面成像站 · 紧凑存储演练", version="1.0.0")
store = Store(data_dir=DATA_DIR, crash_mode=CRASH_MODE)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "crash_mode": CRASH_MODE, "data_dir": DATA_DIR}


@app.get("/api/recovery")
def recovery_status() -> Dict[str, Any]:
    return store.last_recovery


@app.post("/api/admin/reopen")
def reopen() -> Dict[str, Any]:
    """Simulate power coming back: run startup convergence in-process."""
    return store.recover_all()


@app.post("/api/drills", status_code=201)
def create_drill(payload: DrillIn) -> Dict[str, Any]:
    drill = store.create_drill(
        payload.name, [a.model_dump() for a in payload.artifacts]
    )
    return store.drill_view(drill["id"])


@app.get("/api/drills")
def list_drills() -> Dict[str, Any]:
    return {
        "drills": [
            store.drill_view(drill_id)
            for drill_id in sorted(store.registry["drills"])
        ]
    }


@app.get("/api/drills/{drill_id}")
def get_drill(drill_id: str) -> Dict[str, Any]:
    if drill_id not in store.registry["drills"]:
        raise HTTPException(status_code=404, detail="drill not found")
    return store.drill_view(drill_id)


@app.put("/api/drills/{drill_id}/artifacts")
def re_register(drill_id: str, payload: DrillIn) -> Dict[str, Any]:
    """Re-register an artifact set on an existing drill for a new generation."""
    if drill_id not in store.registry["drills"]:
        raise HTTPException(status_code=404, detail="drill not found")
    store.re_register_artifacts(
        drill_id, [a.model_dump() for a in payload.artifacts]
    )
    return store.drill_view(drill_id)


@app.post("/api/drills/{drill_id}/compact")
def compact(drill_id: str, payload: CompactRequest) -> JSONResponse:
    if drill_id not in store.registry["drills"]:
        raise HTTPException(status_code=404, detail="drill not found")
    try:
        result = store.compact(
            drill_id, payload.consolidation_id, payload.crash_after
        )
    except Rejected as exc:
        active = store.registry["drills"][drill_id].get("active_generation")
        return JSONResponse(
            status_code=409,
            content={
                "status": "rejected",
                "consolidation_id": payload.consolidation_id,
                "reject": {
                    "rejected": True,
                    "reason": exc.reason,
                    "code": exc.code,
                    "active_generation": active,
                },
                "drill": store.drill_view(drill_id),
            },
        )
    except SimulatedPowerLoss as exc:
        point = exc.args[0]
        return JSONResponse(
            status_code=503,
            content={
                "status": "simulated_crash",
                "consolidation_id": payload.consolidation_id,
                "crash_point": point,
                "note": (
                    "断电发生在新段落盘之后、目录切换之前。"
                    if point == "segments"
                    else "断电发生在目录切换之后、旧段清扫之前。"
                )
                + "请调用 /api/admin/reopen 模拟恢复（或重启进程）。",
                "drill": store.drill_view(drill_id),
            },
        )
    view = store.drill_view(drill_id)
    return JSONResponse(
        status_code=200,
        content={
            "status": result["status"],
            "consolidation_id": result["consolidation_id"],
            "generation": result["generation"],
            "retransmission": result.get("retransmission", False),
            "swept": result.get("swept", []),
            "previous_generation": result.get("previous_generation"),
            "drill": view,
        },
    )


@app.get("/api/drills/{drill_id}/artifacts/{index}/reassemble")
def reassemble(drill_id: str, index: int) -> Dict[str, Any]:
    """Proof endpoint: rebuild an artifact's text out of its segment."""
    if drill_id not in store.registry["drills"]:
        raise HTTPException(status_code=404, detail="drill not found")
    view = store.drill_view(drill_id)
    if not (0 <= index < len(view["artifacts"])):
        raise HTTPException(status_code=404, detail="artifact not found")
    cat = store._read_current(drill_id)
    if cat is None:
        raise HTTPException(status_code=409, detail="no active catalog generation")
    art = view["artifacts"][index]
    segid = cat["segment"]
    parts = []
    for f in art["fragments"]:
        chunk = store._read_fragment(segid, f["digest"])
        if chunk is None:
            raise HTTPException(status_code=409, detail="fragment missing in segment")
        parts.append(chunk.decode("utf-8"))
    return {
        "artifact": art["name"],
        "segment": segid,
        "text": "".join(parts),
        "reassembled_digest": art["reassembled_digest"],
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

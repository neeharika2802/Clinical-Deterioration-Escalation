"""
VIGIL FastAPI + WebSocket service layer.

Provides:
- POST /observations
- GET /cohort
- GET /patients/{patient_id}/alerts
- POST /patients/{patient_id}/actions
- WebSocket /ws/cohort

The API uses the same patient profiles, guideline retrieval,
pipeline, ranking, clinician commands, and audit logging as
the main VIGIL demo.
"""

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from vigil.state.patient_state import PatientState
from vigil.pipeline import process_observation
from vigil.ranking.cohort_ranker import rank_cohort
from vigil.evidence.commands import run_command
from vigil.models import Observation
from vigil.agents.events import EventBus
from vigil.audit.sqlite_logger import SQLiteAuditLogger


app = FastAPI(
    title="VIGIL API",
    version="0.1.0"
)


def load_profiles():
    """
    Load the same patient profiles used by the demo and dashboard.
    """
    path = Path("data/profiles/patients.json")

    with path.open("r") as f:
        profiles = json.load(f)

    return {
        p["patient_id"]: p
        for p in profiles
    }


PROFILES = load_profiles()


def load_guideline_index():
    """
    Load the same TF-IDF guideline index used by the dashboard.

    If retrieval dependencies are unavailable, the API can still
    run without guideline retrieval.
    """
    try:
        from vigil.agents.retrieval import GuidelineIndex

        return GuidelineIndex("data/guidelines")

    except ImportError:
        print(
            "(scikit-learn not available — "
            "running API without guideline retrieval)"
        )

        return None


GUIDELINE_INDEX = load_guideline_index()


# Process-wide in-memory patient state.
# For a hackathon/demo deployment this is sufficient.
PATIENT_STORE = {}


# SQLite audit trail.
AUDIT = SQLiteAuditLogger("data/api_audit.db")


# Event bus used by the API.
BUS = EventBus()

BUS.subscribe_all(
    lambda event_type, **fields:
        AUDIT.log(event_type, **fields)
)


# Connected WebSocket clients.
_WS_CLIENTS = []


class ObservationIn(BaseModel):
    patient_id: str
    timestamp: float

    HR: Optional[float] = None
    RR: Optional[float] = None
    SpO2: Optional[float] = None
    SBP: Optional[float] = None


class ClinicianActionIn(BaseModel):
    patient_id: str
    action: str
    now: float
    reason: str = ""


def _get_or_create_patient(
    patient_id: str,
    profile: dict = None
) -> PatientState:

    if patient_id not in PATIENT_STORE:

        if profile is None:
            profile = PROFILES.get(patient_id, {})

        PATIENT_STORE[patient_id] = PatientState(
            patient_id=patient_id,
            profile=profile
        )

    return PATIENT_STORE[patient_id]


@app.post("/observations")
async def post_observation(payload: ObservationIn):
    """
    Ingest one observation and run it through the complete
    VIGIL pipeline.
    """

    profile = PROFILES.get(
        payload.patient_id,
        {}
    )

    state = _get_or_create_patient(
        payload.patient_id,
        profile
    )

    obs = Observation(
        patient_id=payload.patient_id,
        timestamp=payload.timestamp,
        HR=payload.HR,
        RR=payload.RR,
        SpO2=payload.SpO2,
        SBP=payload.SBP
    )

    result = process_observation(
        state,
        obs,
        guideline_index=GUIDELINE_INDEX,
        patient_store=PATIENT_STORE,
        event_bus=BUS
    )

    # Send the result to connected dashboard/WebSocket clients.
    for client in list(_WS_CLIENTS):

        try:
            await client.send_json(
                {
                    "type": "tick",
                    "patient_id": payload.patient_id,
                    "result": result
                }
            )

        except Exception:

            if client in _WS_CLIENTS:
                _WS_CLIENTS.remove(client)

    return result


@app.get("/cohort")
async def get_cohort():
    """
    Return the ranked cohort worklist.
    """

    rows = []

    for pid, state in PATIENT_STORE.items():

        f = state.last_features or {
            "severity": 0.0,
            "momentum": 0.0,
            "coherence": 0.0
        }

        rows.append(
            {
                "patient_id": pid,
                "severity": f.get("severity", 0.0),
                "momentum": f.get("momentum", 0.0),
                "coherence": f.get("coherence", 0.0),
                "evidence": state.evidence,
                "alert_state": state.alert_state
            }
        )

    ranked = rank_cohort(rows)

    return [
        {
            "patient_id": r["patient_id"],
            "urgency": r["urgency"],
            "evidence": r["evidence"],
            "alert_state": r["alert_state"].value
        }
        for r in ranked
    ]


@app.get("/patients/{patient_id}/alerts")
async def get_alert_history(patient_id: str):

    if patient_id not in PATIENT_STORE:
        return []

    return PATIENT_STORE[
        patient_id
    ].alert_history


@app.post("/patients/{patient_id}/actions")
async def post_clinician_action(
    patient_id: str,
    payload: ClinicianActionIn
):
    """
    Run accept/dismiss/defer/investigate command
    against the patient's state.
    """

    if patient_id not in PATIENT_STORE:

        return {
            "error": "unknown patient_id"
        }

    state = PATIENT_STORE[patient_id]

    command = run_command(
        state,
        payload.action,
        now=payload.now,
        reason=payload.reason,
        event_bus=BUS
    )

    return {
        "action": command.name,
        "resulting_alert_state":
            state.alert_state.value
    }


@app.websocket("/ws/cohort")
async def websocket_cohort(
    websocket: WebSocket
):
    """
    Push every observation/alert tick to connected clients.
    """

    await websocket.accept()

    _WS_CLIENTS.append(websocket)

    try:

        while True:

            await websocket.receive_text()

    except WebSocketDisconnect:

        if websocket in _WS_CLIENTS:
            _WS_CLIENTS.remove(websocket)

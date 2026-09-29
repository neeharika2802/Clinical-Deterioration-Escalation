#!/usr/bin/env python3
"""
Runs the full deterministic pipeline over every patient in
data/profiles/patients.json and prints a ranked worklist + alert cards after
each tick, exactly like the clinician dashboard would. No Streamlit or LLM
required — this is the guaranteed-to-run demo path.

Usage:
    PYTHONPATH=src python3 scripts/run_demo.py
"""

import json
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vigil.state.patient_state import PatientState
from vigil.simulator.streamer import stream_patient
from vigil.pipeline import process_observation
from vigil.ranking.cohort_ranker import rank_cohort
from vigil.audit.logger import AuditLogger


def load_profiles(path="data/profiles/patients.json"):
    return json.loads(Path(path).read_text())


def load_guideline_index():
    """scikit-learn is an optional dependency for retrieval."""
    try:
        from vigil.agents.retrieval import GuidelineIndex
        return GuidelineIndex("data/guidelines")
    except ImportError:
        print("(scikit-learn not installed — running without guideline retrieval)")
        return None


def main():
    profiles = load_profiles()

    states = {
        p["patient_id"]: PatientState(
            patient_id=p["patient_id"],
            profile=p
        )
        for p in profiles
    }

    audit = AuditLogger("data/audit_log.jsonl")
    guideline_index = load_guideline_index()

    streams = {}

    for p in profiles:
        patient_id = p["patient_id"]

        # Use a deterministic seed.
        # Python's built-in hash() changes between program runs.
        seed = zlib.crc32(patient_id.encode("utf-8")) % 1000

        streams[patient_id] = list(
            stream_patient(
                patient_id,
                p["scenario"],
                seed=seed,
                n=20
            )
        )

    n_ticks = max(len(s) for s in streams.values())

    for tick in range(n_ticks):

        for pid, obs_list in streams.items():

            if tick >= len(obs_list):
                continue

            obs = obs_list[tick]
            state = states[pid]

            # audit_logger=audit makes pipeline.py log every observation,
            # state transition, suppression, tool call and alert itself.
            result = process_observation(
                state,
                obs,
                guideline_index=guideline_index,
                audit_logger=audit,
                patient_store=states
            )

            if result["alert"] is not None:
                print(f"\n[tick {tick}] ALERT for {pid}")

                print(
                    f"  {result['alert']['explanation']}"
                )

                print(
                    f"  verifier: {result['alert']['verifier_result']}"
                    f"  reason: {result['alert']['decision_reason']}"
                )

        # Ranked worklist snapshot every 5 ticks
        if tick % 5 == 0:

            rows = []

            for pid, state in states.items():

                f = state.last_features or {
                    "severity": 0.0,
                    "momentum": 0.0,
                    "coherence": 0.0
                }

                rows.append({
                    "patient_id": pid,
                    "severity": f["severity"],
                    "momentum": f["momentum"],
                    "coherence": f["coherence"],
                    "evidence": state.evidence,
                    "alert_state": state.alert_state,
                })

            ranked = rank_cohort(rows)

            print(
                f"\n--- Worklist snapshot @ tick {tick} ---"
            )

            for r in ranked:

                print(
                    f"  {r['patient_id']:8s} "
                    f"urgency={r['urgency']:5.1f} "
                    f"evidence={r['evidence']:.2f} "
                    f"alert_state={r['alert_state'].value}"
                )

    print(
        "\nDone. Full audit trail written to data/audit_log.jsonl"
    )


if __name__ == "__main__":
    main()

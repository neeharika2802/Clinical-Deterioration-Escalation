"""
VIGIL clinician dashboard.

Run with:

    PYTHONPATH=src streamlit run src/vigil/ui/dashboard.py

The dashboard uses the same deterministic pipeline as
scripts/run_demo.py.
"""

import json
import sys
from pathlib import Path

import streamlit as st


# Allow imports from src/
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2])
)


from vigil.state.patient_state import PatientState
from vigil.simulator.streamer import stream_patient
from vigil.pipeline import process_observation
from vigil.ranking.cohort_ranker import rank_cohort
from vigil.audit.logger import AuditLogger
from vigil.evidence.suppression import apply_clinician_action


st.set_page_config(
    page_title="VIGIL — Clinician Worklist",
    layout="wide"
)


@st.cache_resource
def load_guideline_index():

    try:

        from vigil.agents.retrieval import GuidelineIndex

        return GuidelineIndex(
            "data/guidelines"
        )

    except ImportError:

        return None


def _deterministic_seed(
    patient_id: str
) -> int:

    """
    Generate a reproducible seed.

    Python's built-in hash() is randomized between
    processes, so it must not be used here.
    """

    import zlib

    return zlib.crc32(
        patient_id.encode("utf-8")
    ) % 1000


def init_state():

    profiles = json.loads(
        Path(
            "data/profiles/patients.json"
        ).read_text()
    )

    st.session_state.states = {}

    for p in profiles:

        st.session_state.states[
            p["patient_id"]
        ] = PatientState(
            patient_id=p["patient_id"],
            profile=p
        )

    st.session_state.streams = {}

    for p in profiles:

        patient_id = p["patient_id"]

        seed = _deterministic_seed(
            patient_id
        )

        st.session_state.streams[
            patient_id
        ] = list(
            stream_patient(
                patient_id,
                p["scenario"],
                seed=seed,
                n=40
            )
        )

    st.session_state.tick = 0

    st.session_state.audit = AuditLogger(
        "data/audit_log.jsonl"
    )

    st.session_state.alerts_feed = []


if "states" not in st.session_state:

    init_state()


# ---------------------------------------------------------
# Header
# ---------------------------------------------------------

st.title(
    "VIGIL — Clinician Worklist"
)

st.warning(
    "Decision-support prototype for demonstration only. "
    "It is not a diagnostic or treatment system. "
    "All patient data shown here is synthetic."
)


st.caption(
    "Stateful, evidence-accumulating patient deterioration monitoring"
)


# ---------------------------------------------------------
# Controls
# ---------------------------------------------------------

col_a, col_b = st.columns(
    [1, 5]
)


with col_a:

    if st.button(
        "Advance tick ▶"
    ):

        guideline_index = (
            load_guideline_index()
        )

        tick = st.session_state.tick

        for pid, obs_list in (
            st.session_state.streams.items()
        ):

            if tick >= len(obs_list):
                continue

            obs = obs_list[tick]

            state = (
                st.session_state.states[pid]
            )

            result = process_observation(
                state,
                obs,
                guideline_index=guideline_index,
                audit_logger=st.session_state.audit,
                patient_store=st.session_state.states
            )

            if result["alert"] is not None:

                alert = result["alert"]

                st.session_state.alerts_feed.insert(
                    0,
                    {
                        "patient_id": pid,
                        **alert
                    }
                )

        st.session_state.tick += 1


    if st.button("Reset"):

        init_state()


with col_b:

    st.caption(
        f"Simulated tick: "
        f"{st.session_state.tick}"
    )


# ---------------------------------------------------------
# Cohort worklist
# ---------------------------------------------------------

rows = []


for pid, state in (
    st.session_state.states.items()
):

    f = state.last_features or {
        "severity": 0.0,
        "momentum": 0.0,
        "coherence": 0.0
    }

    rows.append(
        {
            "patient_id": pid,
            "severity": f.get(
                "severity",
                0.0
            ),
            "momentum": f.get(
                "momentum",
                0.0
            ),
            "coherence": f.get(
                "coherence",
                0.0
            ),
            "evidence": state.evidence,
            "alert_state": state.alert_state
        }
    )


ranked = rank_cohort(rows)


st.subheader(
    "Cohort worklist (by urgency)"
)


st.table(
    [
        {
            "Patient":
                r["patient_id"],

            "Urgency":
                round(
                    r["urgency"],
                    1
                ),

            "Evidence":
                round(
                    r["evidence"],
                    2
                ),

            "Alert state":
                r["alert_state"].value
        }

        for r in ranked
    ]
)


# ---------------------------------------------------------
# Alert feed
# ---------------------------------------------------------

st.subheader(
    "Alert feed"
)


if len(
    st.session_state.alerts_feed
) == 0:

    st.info(
        "No alerts yet. "
        "Advance the simulated stream."
    )


for a in (
    st.session_state.alerts_feed[:10]
):

    patient_id = a[
        "patient_id"
    ]

    features = a.get(
        "features",
        {}
    )

    verifier = a.get(
        "verifier_result",
        "unknown"
    )

    with st.expander(
        f"{patient_id} — "
        f"{a['physio_state']} "
        f"(verifier: {verifier})"
    ):

        # -------------------------------------------------
        # Alert summary
        # -------------------------------------------------

        st.markdown(
            "### Alert summary"
        )

        st.write(
            a.get(
                "explanation",
                ""
            )
        )


        # -------------------------------------------------
        # Evidence components
        # -------------------------------------------------

        st.markdown(
            "### Evidence accumulated"
        )

        c1, c2, c3, c4 = st.columns(4)

        c1.metric(
            "Severity",
            f"{features.get('severity', 0.0):.2f}"
        )

        c2.metric(
            "Momentum",
            f"{features.get('momentum', 0.0):.2f}"
        )

        c3.metric(
            "Coherence",
            f"{features.get('coherence', 0.0):.2f}"
        )

        c4.metric(
            "Persistence",
            f"{features.get('persistence', 0.0):.2f}"
        )


        c5, c6, c7 = st.columns(3)

        c5.metric(
            "Evidence",
            f"{features.get('evidence', 0.0):.2f}"
        )

        c6.metric(
            "Partial NEWS2",
            str(
                features.get(
                    "partial_news2",
                    0
                )
            )
        )

        c7.metric(
            "Concordant channels",
            str(
                features.get(
                    "k",
                    0
                )
            )
        )


        # -------------------------------------------------
        # Trigger information
        # -------------------------------------------------

        st.markdown(
            "### Trigger information"
        )

        st.write(
            "Physiological state:",
            a.get(
                "physio_state",
                "unknown"
            )
        )

        st.write(
            "Triggering channels:",
            ", ".join(
                a.get(
                    "channels",
                    []
                )
            )
            if a.get("channels")
            else "None"
        )

        st.write(
            "Red flag:",
            a.get(
                "red_flag",
                False
            )
        )

        st.write(
            "Decision reason:",
            a.get(
                "decision_reason",
                ""
            )
        )


        # -------------------------------------------------
        # Retrieval / verification
        # -------------------------------------------------

        st.markdown(
            "### Investigation and verification"
        )

        st.write(
            "Verifier:",
            verifier
        )

        retrieved = a.get(
            "retrieved_context",
            []
        )

        if retrieved:

            st.write(
                "Retrieved context:"
            )

            for item in retrieved:

                st.write(
                    f"- {item}"
                )


        # -------------------------------------------------
        # Clinician actions
        # -------------------------------------------------

        st.markdown(
            "### Clinician action"
        )

        state = (
            st.session_state.states[
                patient_id
            ]
        )

        c1, c2, c3, c4 = st.columns(4)


        if c1.button(
            "Accept",
            key=(
                f"accept-"
                f"{patient_id}-"
                f"{a['timestamp']}"
            )
        ):

            apply_clinician_action(
                state,
                "accept",
                now=a["timestamp"],
                audit_logger=(
                    st.session_state.audit
                )
            )

            st.success(
                "Alert accepted."
            )


        if c2.button(
            "Dismiss",
            key=(
                f"dismiss-"
                f"{patient_id}-"
                f"{a['timestamp']}"
            )
        ):

            apply_clinician_action(
                state,
                "dismiss",
                now=a["timestamp"],
                audit_logger=(
                    st.session_state.audit
                )
            )

            st.success(
                "Alert dismissed."
            )


        if c3.button(
            "Defer",
            key=(
                f"defer-"
                f"{patient_id}-"
                f"{a['timestamp']}"
            )
        ):

            apply_clinician_action(
                state,
                "defer",
                now=a["timestamp"],
                audit_logger=(
                    st.session_state.audit
                )
            )

            st.success(
                "Alert deferred."
            )


        if c4.button(
            "Investigate",
            key=(
                f"investigate-"
                f"{patient_id}-"
                f"{a['timestamp']}"
            )
        ):

            apply_clinician_action(
                state,
                "investigate",
                now=a["timestamp"],
                audit_logger=(
                    st.session_state.audit
                )
            )

            st.success(
                "Investigation requested."
            )

"""LangGraph state (proposal #20). Plain JSON-serialisable lists/dicts only,
so the SqliteSaver checkpointer can persist the state across approval pauses."""
from __future__ import annotations

from typing import TypedDict


class AgentState(TypedDict):
    workspace_id: str
    artifact_id: str
    event: dict                      # ingest event: {"artifact_id", "kind", ...}

    parsed: dict                     # parsed blocks of the artifact
    extracted_facts: list            # raw extractor output
    reviewed_facts: list             # after Reflection self-correction
    dropped_facts: list              # rejected by reflection (with reason)
    pending_disambiguation: list     # entity cold-start queue
    stored_fact_ids: list
    fact_transitions: dict           # new fact id -> previous current fact id

    change_events: list              # [{"change_event_id", "old_value", "new_value", ...}]
    candidates: dict                 # event_id -> candidate list
    conflicts: dict                  # event_id -> verified conflict list
    impacts: dict                    # event_id -> impact list
    plan: dict                       # {"plan_id", "actions": [...]}

    approval: dict                   # {"decision": "approved"|"rejected", "decisions": {...}}
    execution_results: list
    post_verification: list
    errors: list

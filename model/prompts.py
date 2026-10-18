# ============================================================
# COMPONENT: C6 - Prompts & Schema
# PURPOSE: System prompt, user-message builder, repair prompt, and JSON schema extractor for the analysis harness.
# INPUT: EvidencePack (from C4), Assessment pydantic model (from C0).
# OUTPUT: Prompt strings and JSON schema dict for LLM calls.
# CALLED BY: model/harness.py      CALLS: common/models.py (Assessment)
# IF IT BREAKS, CHECK: Assessment schema changes in common/models.py, passage formatting mismatches.
# ============================================================

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from common.models import Assessment, EvidencePack
from common.trace import ComponentError, trace


def get_assessment_schema() -> dict[str, Any]:
    """IN: None.
    OUT: JSON-schema dict derived from the Assessment pydantic model (excluding component_summaries).
    FAILS WITH: Never raises."""
    # STEP 1: Extract JSON schema from Assessment model | takes: Assessment class | gives: schema dict
    schema = Assessment.model_json_schema()

    # STEP 2: Remove component_summaries from schema properties, required, and defs | takes: schema | gives: pruned schema dict
    if "properties" in schema and "component_summaries" in schema["properties"]:
        del schema["properties"]["component_summaries"]
    if "required" in schema and "component_summaries" in schema["required"]:
        schema["required"].remove("component_summaries")
    if "$defs" in schema and "ComponentSummary" in schema["$defs"]:
        del schema["$defs"]["ComponentSummary"]

    trace("C6", "Extracted Assessment JSON schema", keys=len(schema))
    return schema


# ---------------------------------------------------------------------------
# Schema and Example definitions for prompt injection
# ---------------------------------------------------------------------------
_SCHEMA_COMPACT: str = json.dumps(get_assessment_schema(), separators=(",", ":"))

_EXAMPLE_OBJ: dict[str, Any] = {
    "brief_summary": "<concise evidence-grounded summary>",
    "evidence_status": "supported",
    "hypotheses": [
        {
            "id": "H1",
            "statement": "<clear candidate explanation labeled as hypothesis>",
            "status": "plausible",
            "supporting_evidence": [
                {
                    "evidence_id": "<passage_id from the evidence above>",
                    "quote": "<exact sentence copied from that passage>",
                }
            ],
            "contradicting_evidence": [],
            "reasoning": "<analytical synthesis of how passages bear on hypothesis>",
            "limitations": [
                "<known analytical boundary or data gap>",
            ],
        }
    ],
    "documented_findings": [
        {
            "statement": "<factual claim explicitly stated in source>",
            "evidence_id": "<passage_id from the evidence above>",
            "quote": "<exact sentence copied from that passage>",
            "source_role": "official_finding",
        }
    ],
    "missing_information": [
        "<missing record or data needed>",
    ],
    "suggested_investigative_checks": [
        {
            "check": "<proposed evidence-gathering inspection or check>",
            "rationale": "<justification for why this check is relevant>",
            "evidence_ids": [
                "<passage_id from the evidence above>",
            ],
            "safety_note": "<safety disclaimer for qualified personnel>",
        }
    ],
    "overall_limitations": [
        "<high-level disclaimer or analytical limitation>",
    ],
}

_EXAMPLE_JSON: str = json.dumps(_EXAMPLE_OBJ, indent=2)

_SCHEMA_ESCAPED: str = _SCHEMA_COMPACT.replace("{", "{{").replace("}", "}}")
_EXAMPLE_ESCAPED: str = _EXAMPLE_JSON.replace("{", "{{").replace("}", "}}")


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT – fixed rules injected as the system message in every call.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT: str = f"""\
You are an aviation-investigation evidence analyst. Follow every rule below exactly.

EVIDENCE RULES
1. Use ONLY the supplied evidence passages to support your analysis. Do NOT use outside knowledge for factual claims.
2. Cite every claim by evidence_id and include a short VERBATIM quote copied exactly from that passage.
3. Every quote must be copied exactly from the cited passage; cite only passage ids shown.
4. NEVER invent page numbers, URLs, or source metadata. Those fields are filled by the evidence store.
5. NEVER present a hypothesis as an official finding. Hypotheses are candidate explanations; documented_findings are explicit statements from authoritative sources.
6. Similarity between cases is NOT causation. If evidence is merely analogous, say so.
7. If the evidence is insufficient or conflicting, say so explicitly and set evidence_status to "insufficient" or "contradictory" accordingly.
8. Suggest only non-prescriptive evidence-gathering checks for qualified personnel. Include a safety_note in every suggested check.

OUTPUT RULES
9. Output ONLY a single JSON object matching the Assessment schema. No markdown fences, no commentary outside the JSON.
10. Top-level keys exactly as in the schema, no wrapper key.
11. Do not output case_id, selected_component, or component_summaries (the system fills them).
12. Use controlled values for evidence_status: "supported", "mixed", "insufficient", "contradictory".
13. Use controlled values for hypothesis status: "plausible", "weakly_supported", "contradicted", "unresolved".

EXACT JSON SCHEMA:
{_SCHEMA_COMPACT}

COMPLETE EXAMPLE JSON OBJECT:
{_EXAMPLE_JSON}
"""

# ---------------------------------------------------------------------------
# PLAIN_PROMPT – used for Run-0 (no evidence pack, baseline answer).
# ---------------------------------------------------------------------------
PLAIN_PROMPT: str = """\
Answer the following aviation-investigation question using only your general knowledge.
Do NOT fabricate evidence IDs, page numbers, or URLs.
Return your answer as plain text (not JSON).

Question: {question}
Component: {component}
"""

# ---------------------------------------------------------------------------
# REPAIR_PROMPT – sent on the retry attempt when schema validation fails.
# ---------------------------------------------------------------------------
REPAIR_PROMPT: str = f"""\
Your previous response did not conform to the required JSON schema.
Validation errors:
{{errors}}

Please output a corrected JSON object that fixes ALL the above errors.
Follow every rule from the system prompt. Output ONLY valid JSON, no markdown fences.
Top-level keys exactly as in the schema, no wrapper key.
Do not output case_id, selected_component, or component_summaries (the system fills them).
Every quote must be copied exactly from the cited passage; cite only passage ids shown.

EXACT JSON SCHEMA:
{_SCHEMA_ESCAPED}

COMPLETE EXAMPLE JSON OBJECT:
{_EXAMPLE_ESCAPED}
"""


def build_user_message(pack: EvidencePack) -> str:
    """IN: Validated EvidencePack instance.
    OUT: Formatted user-message string with question, component, and labeled passages.
    FAILS WITH: Never raises (pack is already validated)."""
    # STEP 1: Build header with question and component | takes: pack | gives: header lines
    lines: list[str] = [
        f"Question: {pack.question}",
        f"Component: {pack.component_id}",
        "",
        "=== EVIDENCE PASSAGES ===",
    ]

    # STEP 2: Format each passage with metadata | takes: pack.passages | gives: passage blocks
    for p in pack.passages:
        page_str = str(p.page_number) if p.page_number is not None else "N/A"
        lines.append(
            f"[{p.passage_id}] ({p.document_title}, page {page_str}, "
            f"role={p.evidence_role}): {p.text}"
        )

    user_msg = "\n".join(lines)
    trace("C6", "Built user message", passage_count=len(pack.passages), msg_len=len(user_msg))
    return user_msg


def run_selftest() -> None:
    """IN: None (invoked via --selftest).
    OUT: None (prints PASS/FAIL).
    FAILS WITH: SystemExit on test failure."""
    # STEP 1: Verify get_assessment_schema() does not contain component_summaries | takes: None | gives: schema dict
    schema = get_assessment_schema()
    if "component_summaries" in schema.get("properties", {}):
        print("FAIL: component_summaries present in schema properties")
        sys.exit(1)
    if "component_summaries" in schema.get("required", []):
        print("FAIL: component_summaries present in schema required")
        sys.exit(1)
    if "ComponentSummary" in schema.get("$defs", {}):
        print("FAIL: ComponentSummary present in schema $defs")
        sys.exit(1)

    # STEP 2: Verify SYSTEM_PROMPT contains required instructions and schema | takes: SYSTEM_PROMPT | gives: validation checks
    assert "top-level keys exactly as in the schema, no wrapper key" in SYSTEM_PROMPT.lower()
    assert "do not output case_id, selected_component, or component_summaries" in SYSTEM_PROMPT.lower()
    assert "every quote must be copied exactly from the cited passage" in SYSTEM_PROMPT.lower()
    assert "cite only passage ids shown" in SYSTEM_PROMPT.lower()
    assert _SCHEMA_COMPACT in SYSTEM_PROMPT
    assert "<passage_id from the evidence above>" in SYSTEM_PROMPT
    assert "<exact sentence copied from that passage>" in SYSTEM_PROMPT

    # STEP 3: Verify REPAIR_PROMPT formatting with error message | takes: REPAIR_PROMPT | gives: formatted string
    test_error = "Test validation error line 1\nTest validation error line 2"
    formatted_repair = REPAIR_PROMPT.format(errors=test_error)
    assert test_error in formatted_repair
    assert _SCHEMA_COMPACT in formatted_repair
    assert "<passage_id from the evidence above>" in formatted_repair

    # STEP 4: Verify build_user_message on fixture pack | takes: pack.json fixture | gives: formatted user message
    fixtures_dir = Path(__file__).resolve().parent.parent / "contracts" / "fixtures"
    pack_file = fixtures_dir / "pack.json"
    with open(pack_file, "r", encoding="utf-8") as f:
        pack_raw = json.load(f)
    pack = EvidencePack.model_validate(pack_raw)
    msg = build_user_message(pack)
    assert pack.question in msg
    assert pack.component_id in msg
    for p in pack.passages:
        assert p.passage_id in msg

    trace("C6", "Prompts self-test passed successfully")
    print("PASS")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        print("Usage: python -m model.prompts --selftest")

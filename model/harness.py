# ============================================================
# COMPONENT: C6 - Analysis Harness
# PURPOSE: Orchestrate LLM call, schema validation, repair retry, verification, and RunLog persistence.
# INPUT: EvidencePack, run_label, temperature, optional thinking flag, max_tokens, injectable llm callable.
# OUTPUT: RunLog saved to data/runs/<run_id>.json and returned to caller.
# CALLED BY: Pipeline orchestrator, evaluation scripts      CALLS: model/prompts.py, model/verifier.py, model/llm_client.py (chat), common/models.py, common/config.py
# IF IT BREAKS, CHECK: LLM response format changes, Assessment schema drift, DATA_DIR write permissions, JSON fence stripping.
# ============================================================

from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import ValidationError

from common.config import get_settings
from common.models import (
    Assessment,
    ComponentSummary,
    EvidencePack,
    RunConfig,
    RunLog,
    RunMetrics,
    Verification,
)
from common.trace import ComponentError, trace
from model.llm_client import LLMResult, chat as default_chat
from model.prompts import (
    PLAIN_PROMPT,
    REPAIR_PROMPT,
    SYSTEM_PROMPT,
    build_user_message,
    get_assessment_schema,
)
from model.verifier import apply_downgrades, resolve_references, verify_assessment


def _strip_code_fences(text: str) -> str:
    """IN: Raw LLM output string (may contain markdown code fences).
    OUT: String with leading/trailing code fences removed.
    FAILS WITH: Never raises."""
    # STEP 1: Remove ```json ... ``` or ``` ... ``` wrappers | takes: text | gives: clean JSON string
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped)
    stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped.strip()


def _now_iso() -> str:
    """IN: None.
    OUT: ISO-8601 UTC timestamp string.
    FAILS WITH: Never raises."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_run_id() -> str:
    """IN: None.
    OUT: Unique run identifier string.
    FAILS WITH: Never raises."""
    return f"run-{uuid.uuid4().hex[:12]}"


def _save_run_log(run_log: RunLog, runs_dir: Optional[Path] = None) -> Path:
    """IN: Completed RunLog; optional explicit runs_dir (used by self-tests).
    OUT: Path where the RunLog JSON was saved.
    FAILS WITH: ComponentError if settings fail to load or DATA_DIR is not writable."""
    # STEP 1: Resolve runs directory from explicit arg or config | takes: runs_dir, settings | gives: resolved runs_dir
    if runs_dir is None:
        settings = get_settings()  # raises ComponentError on failure — no silent fallback
        runs_dir = settings.DATA_DIR / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # STEP 2: Write RunLog JSON to disk | takes: run_log, runs_dir | gives: saved file path
    out_path = runs_dir / f"{run_log.run_id}.json"
    try:
        out_path.write_text(run_log.model_dump_json(indent=2), encoding="utf-8")
        trace("C6", "RunLog saved", run_id=run_log.run_id, path=str(out_path))
    except Exception as exc:
        raise ComponentError("C6", "save_run_log", f"Failed to write RunLog to {out_path}: {exc}") from exc
    return out_path


def _prepare_raw_parsed(parsed: Any, pack: EvidencePack) -> Any:
    """IN: Raw parsed JSON object (dict or other) and EvidencePack.
    OUT: Preprocessed dictionary unwrapped and populated with case/component IDs, discarding model component_summaries.
    FAILS WITH: Never raises."""
    # STEP 1: Inspect parsed structure and unwrap single top-level assessment key | takes: parsed | gives: unwrapped dict or original
    if not isinstance(parsed, dict):
        return parsed

    data: dict[str, Any] = parsed
    if len(data) == 1 and "assessment" in data and isinstance(data["assessment"], dict):
        data = data["assessment"]
        trace("C6", "Unwrapped top-level assessment wrapper")

    if not isinstance(data, dict):
        return data

    # STEP 2: Inject pack IDs and discard model-written component_summaries | takes: data, pack | gives: prepared dict
    data["case_id"] = pack.case_id
    data["selected_component"] = pack.component_id
    if "component_summaries" in data:
        data.pop("component_summaries", None)
        trace("C6", "Discarded model-written component_summaries")

    trace("C6", "Prepared raw parsed dictionary", case_id=data["case_id"], component_id=data["selected_component"])
    return data


def run_analysis(
    pack: EvidencePack,
    run_label: str,
    temperature: float = 0.0,
    thinking: Optional[bool] = None,
    use_server_schema: bool = False,
    max_tokens: int = 4000,
    llm: Callable[..., LLMResult] = default_chat,
    runs_dir: Optional[Path] = None,
) -> RunLog:
    """IN: EvidencePack, run_label ("0","A","B","C","live"), temperature float, thinking flag, server-schema flag, max_tokens int, injectable llm callable, optional runs_dir.
    OUT: RunLog with assessment, verification, metrics, and raw_output (saved to disk).
    FAILS WITH: Returns RunLog with error field on model/parse/empty-pack failures. Raises ComponentError only for programming errors."""
    run_id = _make_run_id()
    timestamp = _now_iso()

    # STEP 1: Determine run config | takes: temperature, thinking, max_tokens | gives: RunConfig
    run_config = RunConfig(
        temperature=temperature,
        thinking=bool(thinking),
        max_model_len=0,  # filled from result if available
        other={"max_tokens": max_tokens},
    )

    # ---------- Run 0: plain-text mode (ONLY if run_label == "0") ----------
    # STEP 2: Handle Run 0 or empty pack | takes: pack, run_label | gives: early RunLog or continue
    if run_label == "0":
        trace("C6", "Run label '0' – using plain-text mode", run_label=run_label)
        plain_prompt = PLAIN_PROMPT.format(question=pack.question, component=pack.component_id)
        messages = [{"role": "user", "content": plain_prompt}]
        try:
            result = llm(
                messages=messages,
                temperature=temperature,
                thinking=thinking,
                max_tokens=max_tokens,
            )
            raw_output = result.text
            metrics = RunMetrics(
                latency_s=result.latency_s,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                tokens_per_s=result.tokens_per_s,
            )
            model_name = result.model_name
        except Exception as exc:
            raw_output = ""
            metrics = RunMetrics(latency_s=0.0, input_tokens=0, output_tokens=0, tokens_per_s=0.0)
            model_name = "unknown"
            error_msg = f"LLM call failed in plain mode: {exc}"
            trace("C6", error_msg)
            rl = RunLog(
                run_id=run_id, run_label=run_label, model_name=model_name,
                config=run_config, pack_id=pack.pack_id, raw_output=raw_output,
                assessment=None, verification=None, metrics=metrics,
                timestamp=timestamp, error=error_msg,
            )
            _save_run_log(rl, runs_dir=runs_dir)
            return rl

        rl = RunLog(
            run_id=run_id, run_label="0", model_name=model_name,
            config=run_config, pack_id=pack.pack_id, raw_output=raw_output,
            assessment=None, verification=None, metrics=metrics,
            timestamp=timestamp, error=None,
        )
        _save_run_log(rl, runs_dir=runs_dir)
        return rl

    if not pack.passages:
        error_msg = "no evidence available for this component"
        trace("C6", "Empty pack with non-zero run_label – aborting without LLM call", run_label=run_label)
        rl = RunLog(
            run_id=run_id, run_label=run_label, model_name="none",
            config=run_config, pack_id=pack.pack_id, raw_output="",
            assessment=None, verification=None,
            metrics=RunMetrics(latency_s=0.0, input_tokens=0, output_tokens=0, tokens_per_s=0.0),
            timestamp=timestamp, error=error_msg,
        )
        _save_run_log(rl, runs_dir=runs_dir)
        return rl

    # ---------- Structured analysis path ----------
    # STEP 3: Build messages | takes: pack | gives: messages list
    user_msg = build_user_message(pack)
    json_schema = get_assessment_schema() if use_server_schema else None
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    trace("C6", "Messages built for structured analysis", message_count=len(messages))

    # STEP 4: Call LLM | takes: messages, temperature, thinking, max_tokens, json_schema | gives: LLMResult
    try:
        result = llm(
            messages=messages,
            temperature=temperature,
            thinking=thinking,
            max_tokens=max_tokens,
            json_schema=json_schema,
        )
        raw_output = result.text
        model_name = result.model_name
        metrics = RunMetrics(
            latency_s=result.latency_s,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tokens_per_s=result.tokens_per_s,
        )
    except Exception as exc:
        error_msg = f"LLM call failed: {exc}"
        trace("C6", error_msg)
        rl = RunLog(
            run_id=run_id, run_label=run_label, model_name="unknown",
            config=run_config, pack_id=pack.pack_id, raw_output="",
            assessment=None, verification=None,
            metrics=RunMetrics(latency_s=0.0, input_tokens=0, output_tokens=0, tokens_per_s=0.0),
            timestamp=timestamp, error=error_msg,
        )
        _save_run_log(rl, runs_dir=runs_dir)
        return rl

    # STEP 5: Parse JSON from raw output | takes: raw_output | gives: parsed dict or error
    retries_used = 0
    assessment: Optional[Assessment] = None
    parse_error: Optional[str] = None

    clean_text = _strip_code_fences(raw_output)
    try:
        parsed = json.loads(clean_text)
        prepared = _prepare_raw_parsed(parsed, pack)
        assessment = Assessment.model_validate(prepared)
        trace("C6", "Assessment parsed successfully on first attempt")
    except (json.JSONDecodeError, ValidationError) as exc:
        parse_error = str(exc)
        trace("C6", "First parse failed, attempting repair retry", error=parse_error[:500])

        # STEP 6: Retry ONCE with REPAIR_PROMPT | takes: parse_error | gives: repaired Assessment or final error
        repair_msg = REPAIR_PROMPT.format(errors=parse_error)
        messages.append({"role": "assistant", "content": raw_output})
        messages.append({"role": "user", "content": repair_msg})

        try:
            retry_result = llm(
                messages=messages,
                temperature=temperature,
                thinking=thinking,
                max_tokens=max_tokens,
                json_schema=json_schema,
            )
            retries_used = 1
            raw_output_retry = retry_result.text
            # Update metrics to include retry
            metrics = RunMetrics(
                latency_s=metrics.latency_s + retry_result.latency_s,
                input_tokens=metrics.input_tokens + retry_result.input_tokens,
                output_tokens=metrics.output_tokens + retry_result.output_tokens,
                tokens_per_s=round(
                    (metrics.output_tokens + retry_result.output_tokens)
                    / (metrics.latency_s + retry_result.latency_s), 2
                ) if (metrics.latency_s + retry_result.latency_s) > 0 else 0.0,
            )

            clean_retry = _strip_code_fences(raw_output_retry)
            parsed_retry = json.loads(clean_retry)
            prepared_retry = _prepare_raw_parsed(parsed_retry, pack)
            assessment = Assessment.model_validate(prepared_retry)
            raw_output = raw_output + "\n---RETRY---\n" + raw_output_retry
            parse_error = None
            trace("C6", "Assessment parsed successfully on repair retry")
        except Exception as retry_exc:
            retries_used = 1
            raw_output = raw_output + "\n---RETRY_FAILED---\n" + str(retry_exc)
            parse_error = str(retry_exc)
            trace("C6", "Repair retry failed", error=parse_error[:500])

    # STEP 7: If assessment is None (both attempts failed), return error RunLog | takes: parse_error | gives: RunLog
    if assessment is None:
        trace("C6", "Analysis failed due to validation errors", error=parse_error[:500] if parse_error else "")
        rl = RunLog(
            run_id=run_id, run_label=run_label, model_name=model_name,
            config=run_config, pack_id=pack.pack_id, raw_output=raw_output,
            assessment=None,
            verification=Verification(status="failed", claim_checks=[], retries_used=retries_used),
            metrics=metrics, timestamp=timestamp,
            error=parse_error,
        )
        _save_run_log(rl, runs_dir=runs_dir)
        return rl

    # STEP 8: Verify and apply downgrades | takes: assessment, pack | gives: verified assessment
    verification = verify_assessment(assessment, pack)
    verification.retries_used = retries_used
    assessment = apply_downgrades(assessment, verification)

    # STEP 9: Rebuild component_summaries discarding anything the model wrote | takes: assessment, pack | gives: assessment with rebuilt component_summaries
    assessment.component_summaries = {
        pack.component_id: ComponentSummary(
            evidence_status=assessment.evidence_status,
            passage_count=len(pack.passages),
        )
    }
    trace("C6", "Rebuilt component_summaries", component=pack.component_id, passage_count=len(pack.passages))

    # STEP 10: Resolve references from pack | takes: assessment, pack | gives: final assessment
    assessment = resolve_references(assessment, pack)

    # STEP 11: Assemble and save RunLog | takes: all components | gives: RunLog
    rl = RunLog(
        run_id=run_id, run_label=run_label, model_name=model_name,
        config=run_config, pack_id=pack.pack_id, raw_output=raw_output,
        assessment=assessment, verification=verification,
        metrics=metrics, timestamp=timestamp, error=None,
    )
    _save_run_log(rl, runs_dir=runs_dir)
    return rl


# ========================= SELF-TEST =========================

def _make_test_pack() -> EvidencePack:
    """Build a minimal test pack from the fixture data."""
    from common.models import EvidencePassage
    return EvidencePack(
        pack_id="test-pack-001",
        case_id="case_test_01",
        component_id="landing_gear",
        question="What caused the landing gear failure?",
        created_at="2026-10-15T10:00:00Z",
        token_budget=4000,
        approx_tokens=200,
        passages=[
            EvidencePassage(
                passage_id="P001",
                document_title="Factual Report - Landing Gear",
                page_number=4,
                source_url="https://example.com/report.pdf",
                evidence_role="observation",
                text="Post-flight inspection revealed that the main landing gear downlock pin had fractured along the shear plane.",
            ),
            EvidencePassage(
                passage_id="P002",
                document_title="Factual Report - Metallurgy",
                page_number=7,
                source_url="https://example.com/report.pdf",
                evidence_role="official_finding",
                text="Metallurgical laboratory analysis confirmed micro-void coalescence and stress corrosion cracking in the actuator cylinder.",
            ),
        ],
    )


def _make_empty_pack() -> EvidencePack:
    """Build an empty pack for Run-0 / empty-pack testing."""
    return EvidencePack(
        pack_id="test-pack-empty",
        case_id="case_test_01",
        component_id="landing_gear",
        question="What caused the landing gear failure?",
        created_at="2026-10-15T10:00:00Z",
        token_budget=4000,
        approx_tokens=0,
        passages=[],
    )


def _valid_assessment_json(pack: EvidencePack) -> str:
    """Return a valid Assessment JSON string using passages from the pack."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Mechanical failure in downlock assembly.",
        "evidence_status": "mixed",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "Stress corrosion caused extension resistance.",
                "status": "plausible",
                "supporting_evidence": [
                    {"evidence_id": "P002", "quote": "Metallurgical laboratory analysis confirmed micro-void coalescence and stress corrosion cracking in the actuator cylinder."}
                ],
                "contradicting_evidence": [],
                "reasoning": "Lab analysis supports metallurgical fatigue.",
                "limitations": ["Sequence not established."],
            }
        ],
        "documented_findings": [
            {
                "statement": "Downlock pin fractured along shear plane.",
                "evidence_id": "P001",
                "quote": "Post-flight inspection revealed that the main landing gear downlock pin had fractured along the shear plane.",
                "source_role": "observation",
            }
        ],
        "missing_information": ["FDR data needed."],
        "suggested_investigative_checks": [
            {
                "check": "Ultrasonic test of fleet actuators.",
                "rationale": "Detect latent cracking.",
                "evidence_ids": ["P002"],
                "safety_note": "For qualified personnel only.",
            }
        ],
        "component_summaries": {
            "landing_gear": {"evidence_status": "mixed", "passage_count": 2}
        },
        "overall_limitations": ["Investigation support only."],
    })


def _assessment_with_bad_id(pack: EvidencePack) -> str:
    """Assessment referencing a passage_id NOT in the pack."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Summary.",
        "evidence_status": "supported",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "Some hypothesis.",
                "status": "plausible",
                "supporting_evidence": [
                    {"evidence_id": "P999", "quote": "This is a sufficiently long nonexistent passage quote for testing purposes."}
                ],
                "contradicting_evidence": [],
                "reasoning": "Reasoning.",
                "limitations": [],
            }
        ],
        "documented_findings": [],
        "missing_information": [],
        "suggested_investigative_checks": [],
        "component_summaries": {
            "landing_gear": {"evidence_status": "supported", "passage_count": 2}
        },
        "overall_limitations": [],
    })


def _assessment_with_bad_quote(pack: EvidencePack) -> str:
    """Assessment where the quote does NOT appear in the passage text."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Summary.",
        "evidence_status": "supported",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "Some hypothesis.",
                "status": "plausible",
                "supporting_evidence": [
                    {"evidence_id": "P001", "quote": "This quote is completely fabricated and does not appear anywhere in the passage."}
                ],
                "contradicting_evidence": [],
                "reasoning": "Reasoning.",
                "limitations": [],
            }
        ],
        "documented_findings": [],
        "missing_information": [],
        "suggested_investigative_checks": [],
        "component_summaries": {
            "landing_gear": {"evidence_status": "supported", "passage_count": 2}
        },
        "overall_limitations": [],
    })


def _assessment_with_empty_quote(pack: EvidencePack) -> str:
    """Assessment where a citation has an empty quote."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Summary.",
        "evidence_status": "supported",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "Hypothesis with empty quote.",
                "status": "plausible",
                "supporting_evidence": [
                    {"evidence_id": "P001", "quote": ""}
                ],
                "contradicting_evidence": [],
                "reasoning": "Reasoning.",
                "limitations": [],
            }
        ],
        "documented_findings": [],
        "missing_information": [],
        "suggested_investigative_checks": [],
        "component_summaries": {
            "landing_gear": {"evidence_status": "supported", "passage_count": 2}
        },
        "overall_limitations": [],
    })


def _assessment_with_short_quote(pack: EvidencePack) -> str:
    """Assessment where a citation has a one-word / too-short quote."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Summary.",
        "evidence_status": "supported",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "Hypothesis with short quote.",
                "status": "plausible",
                "supporting_evidence": [
                    {"evidence_id": "P001", "quote": "fractured"}
                ],
                "contradicting_evidence": [],
                "reasoning": "Reasoning.",
                "limitations": [],
            }
        ],
        "documented_findings": [],
        "missing_information": [],
        "suggested_investigative_checks": [],
        "component_summaries": {
            "landing_gear": {"evidence_status": "supported", "passage_count": 2}
        },
        "overall_limitations": [],
    })


def _assessment_with_no_citations(pack: EvidencePack) -> str:
    """Assessment where hypotheses and findings have zero citations."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Summary with no citations anywhere.",
        "evidence_status": "supported",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "A hypothesis with no supporting or contradicting evidence.",
                "status": "plausible",
                "supporting_evidence": [],
                "contradicting_evidence": [],
                "reasoning": "No evidence cited.",
                "limitations": [],
            }
        ],
        "documented_findings": [],
        "missing_information": [],
        "suggested_investigative_checks": [],
        "component_summaries": {
            "landing_gear": {"evidence_status": "supported", "passage_count": 2}
        },
        "overall_limitations": [],
    })


def _assessment_with_extra_components(pack: EvidencePack) -> str:
    """Assessment with component_summaries for components NOT in the pack."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Mechanical failure.",
        "evidence_status": "mixed",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "Stress corrosion caused extension resistance.",
                "status": "plausible",
                "supporting_evidence": [
                    {"evidence_id": "P002", "quote": "Metallurgical laboratory analysis confirmed micro-void coalescence and stress corrosion cracking in the actuator cylinder."}
                ],
                "contradicting_evidence": [],
                "reasoning": "Lab analysis supports metallurgical fatigue.",
                "limitations": ["Sequence not established."],
            }
        ],
        "documented_findings": [],
        "missing_information": [],
        "suggested_investigative_checks": [],
        "component_summaries": {
            "landing_gear": {"evidence_status": "mixed", "passage_count": 99},
            "engine": {"evidence_status": "insufficient", "passage_count": 0},
            "wing_flaps": {"evidence_status": "insufficient", "passage_count": 0},
        },
        "overall_limitations": [],
    })


def _assessment_with_bad_check_id(pack: EvidencePack) -> str:
    """Assessment where a suggested_investigative_check cites an unknown passage ID."""
    return json.dumps({
        "case_id": pack.case_id,
        "selected_component": pack.component_id,
        "brief_summary": "Summary.",
        "evidence_status": "supported",
        "hypotheses": [
            {
                "id": "H1",
                "statement": "Hypothesis.",
                "status": "plausible",
                "supporting_evidence": [
                    {"evidence_id": "P001", "quote": "Post-flight inspection revealed that the main landing gear downlock pin had fractured along the shear plane."}
                ],
                "contradicting_evidence": [],
                "reasoning": "Reasoning.",
                "limitations": [],
            }
        ],
        "documented_findings": [],
        "missing_information": [],
        "suggested_investigative_checks": [
            {
                "check": "Check something.",
                "rationale": "Because reasons.",
                "evidence_ids": ["P001", "P_UNKNOWN"],
                "safety_note": "For qualified personnel only.",
            }
        ],
        "component_summaries": {
            "landing_gear": {"evidence_status": "supported", "passage_count": 2}
        },
        "overall_limitations": [],
    })


def run_selftest() -> None:
    """IN: None (invoked via --selftest).
    OUT: None (prints PASS/FAIL per test case and exits).
    FAILS WITH: SystemExit on test failure."""
    import os
    import tempfile

    # Ensure DATA_DIR is set so trace.py can write
    os.environ.setdefault("DATA_DIR", "data")
    # Also set required env vars to avoid config loading failures during save
    for var in ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
                "EVOLUS_BASE_URL", "EVOLUS_API_KEY", "EVOLUS_APP_ID"]:
        os.environ.setdefault(var, "selftest_placeholder")

    # Use a temporary directory so self-tests never write to data/runs
    tmp_runs = Path(tempfile.mkdtemp(prefix="c6_selftest_runs_"))

    pack = _make_test_pack()
    results: list[tuple[str, bool, str]] = []

    # --- Helper: fake llm that returns a fixed string ---
    def _make_fake_llm(response_text: str) -> Callable[..., LLMResult]:
        def fake_llm(**kwargs: Any) -> LLMResult:
            return LLMResult(
                text=response_text,
                input_tokens=10,
                output_tokens=50,
                latency_s=0.5,
                tokens_per_s=100.0,
                model_name="test-model",
            )
        return fake_llm

    call_count = 0

    def _make_counting_llm(responses: list[str]) -> Callable[..., LLMResult]:
        """Returns successive responses on each call."""
        nonlocal call_count
        call_count = 0

        def fake_llm(**kwargs: Any) -> LLMResult:
            nonlocal call_count
            idx = min(call_count, len(responses) - 1)
            call_count += 1
            return LLMResult(
                text=responses[idx],
                input_tokens=10,
                output_tokens=50,
                latency_s=0.5,
                tokens_per_s=100.0,
                model_name="test-model",
            )
        return fake_llm

    # ---- Case 1: Valid output -> verified ----
    try:
        valid_json = _valid_assessment_json(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(valid_json), runs_dir=tmp_runs)
        assert rl.assessment is not None, "assessment should not be None"
        assert rl.verification is not None, "verification should not be None"
        assert rl.verification.status == "verified", f"expected verified, got {rl.verification.status}"
        assert rl.error is None, f"expected no error, got {rl.error}"
        results.append(("case1_valid_verified", True, "PASS"))
    except Exception as exc:
        results.append(("case1_valid_verified", False, f"FAIL: {exc}"))

    # ---- Case 2: Unknown evidence_id -> downgraded, status partial ----
    try:
        bad_id_json = _assessment_with_bad_id(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(bad_id_json), runs_dir=tmp_runs)
        assert rl.verification is not None, "verification should not be None"
        assert rl.verification.status == "partial", f"expected partial, got {rl.verification.status}"
        downgraded_checks = [c for c in rl.verification.claim_checks if c.downgraded]
        assert len(downgraded_checks) > 0, "expected at least one downgraded check"
        # evidence_status should have been lowered
        assert rl.assessment is not None
        assert rl.assessment.hypotheses[0].status == "unresolved", f"expected unresolved, got {rl.assessment.hypotheses[0].status}"
        results.append(("case2_unknown_id_downgraded", True, "PASS"))
    except Exception as exc:
        results.append(("case2_unknown_id_downgraded", False, f"FAIL: {exc}"))

    # ---- Case 3: Quote not in passage -> downgraded ----
    try:
        bad_quote_json = _assessment_with_bad_quote(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(bad_quote_json), runs_dir=tmp_runs)
        assert rl.verification is not None, "verification should not be None"
        assert rl.verification.status == "partial", f"expected partial, got {rl.verification.status}"
        downgraded_checks = [c for c in rl.verification.claim_checks if c.downgraded]
        assert len(downgraded_checks) > 0, "expected at least one downgraded check"
        assert downgraded_checks[0].id_exists is True, "id should exist"
        assert downgraded_checks[0].quote_found is False, "quote should not be found"
        results.append(("case3_bad_quote_downgraded", True, "PASS"))
    except Exception as exc:
        results.append(("case3_bad_quote_downgraded", False, f"FAIL: {exc}"))

    # ---- Case 4: Invalid JSON first, valid on retry -> retries_used == 1 ----
    try:
        valid_json = _valid_assessment_json(pack)
        fake_llm = _make_counting_llm(["THIS IS NOT JSON {{{", valid_json])
        rl = run_analysis(pack, "A", llm=fake_llm, runs_dir=tmp_runs)
        assert rl.assessment is not None, "assessment should not be None after retry"
        assert rl.verification is not None, "verification should not be None"
        assert rl.verification.retries_used == 1, f"expected retries_used==1, got {rl.verification.retries_used}"
        assert rl.error is None, f"expected no error, got {rl.error}"
        results.append(("case4_retry_success", True, "PASS"))
    except Exception as exc:
        results.append(("case4_retry_success", False, f"FAIL: {exc}"))

    # ---- Case 5: Invalid twice -> RunLog with error ----
    try:
        fake_llm = _make_counting_llm(["NOT JSON", "STILL NOT JSON"])
        rl = run_analysis(pack, "A", llm=fake_llm, runs_dir=tmp_runs)
        assert rl.assessment is None, "assessment should be None"
        assert rl.error is not None, "expected error in RunLog"
        assert rl.verification is not None
        assert rl.verification.status == "failed", f"expected failed, got {rl.verification.status}"
        assert rl.verification.retries_used == 1, f"expected retries_used==1, got {rl.verification.retries_used}"
        results.append(("case5_double_failure", True, "PASS"))
    except Exception as exc:
        results.append(("case5_double_failure", False, f"FAIL: {exc}"))

    # ---- Case 6: Empty pack -> plain mode (run_label "0") ----
    try:
        empty_pack = _make_empty_pack()
        rl = run_analysis(empty_pack, "0", llm=_make_fake_llm("Plain text answer with no evidence."), runs_dir=tmp_runs)
        assert rl.run_label == "0", f"expected run_label '0', got '{rl.run_label}'"
        assert rl.assessment is None, "assessment should be None in plain mode"
        assert rl.verification is None, "verification should be None in plain mode"
        assert "Plain text answer" in rl.raw_output, "raw_output should contain the plain answer"
        assert rl.error is None, f"expected no error, got {rl.error}"
        results.append(("case6_empty_pack_plain", True, "PASS"))
    except Exception as exc:
        results.append(("case6_empty_pack_plain", False, f"FAIL: {exc}"))

    # ---- Case 7: Empty quote -> downgraded with "quote empty/too short" ----
    try:
        eq_json = _assessment_with_empty_quote(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(eq_json), runs_dir=tmp_runs)
        assert rl.verification is not None
        assert rl.verification.status == "partial", f"expected partial, got {rl.verification.status}"
        dc = [c for c in rl.verification.claim_checks if c.downgraded]
        assert len(dc) > 0, "expected downgraded check for empty quote"
        assert any("empty/too short" in (c.reason or "") for c in dc), f"expected 'empty/too short' reason, got {[c.reason for c in dc]}"
        results.append(("case7_empty_quote", True, "PASS"))
    except Exception as exc:
        results.append(("case7_empty_quote", False, f"FAIL: {exc}"))

    # ---- Case 8: One-word (too short) quote -> downgraded with "quote empty/too short" ----
    try:
        sq_json = _assessment_with_short_quote(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(sq_json), runs_dir=tmp_runs)
        assert rl.verification is not None
        assert rl.verification.status == "partial", f"expected partial, got {rl.verification.status}"
        dc = [c for c in rl.verification.claim_checks if c.downgraded]
        assert len(dc) > 0, "expected downgraded check for short quote"
        assert any("empty/too short" in (c.reason or "") for c in dc), f"expected 'empty/too short' reason, got {[c.reason for c in dc]}"
        results.append(("case8_short_quote", True, "PASS"))
    except Exception as exc:
        results.append(("case8_short_quote", False, f"FAIL: {exc}"))

    # ---- Case 9: No citations at all -> status "failed" ----
    try:
        nc_json = _assessment_with_no_citations(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(nc_json), runs_dir=tmp_runs)
        assert rl.verification is not None
        dc = [c for c in rl.verification.claim_checks if c.downgraded]
        assert len(dc) > 0, "expected downgraded check for no-citation hypothesis"
        assert any("no citation" in (c.reason or "") for c in dc), f"expected 'no citation' reason, got {[c.reason for c in dc]}"
        assert rl.assessment is not None
        assert rl.assessment.hypotheses[0].status == "unresolved", f"expected unresolved, got {rl.assessment.hypotheses[0].status}"
        results.append(("case9_no_citations", True, "PASS"))
    except Exception as exc:
        results.append(("case9_no_citations", False, f"FAIL: {exc}"))

    # ---- Case 10: component_summaries sync — evidence_status matches assessment & other components removed ----
    try:
        ec_json = _assessment_with_extra_components(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(ec_json), runs_dir=tmp_runs)
        assert rl.assessment is not None
        cs = rl.assessment.component_summaries
        # Only the pack's component should remain
        assert list(cs.keys()) == ["landing_gear"], f"expected only landing_gear, got {list(cs.keys())}"
        # passage_count must come from the pack (2 passages), not the model (99)
        assert cs["landing_gear"].passage_count == 2, f"expected passage_count=2, got {cs['landing_gear'].passage_count}"
        # evidence_status must equal the assessment's evidence_status
        assert cs["landing_gear"].evidence_status == rl.assessment.evidence_status, \
            f"expected component evidence_status={rl.assessment.evidence_status}, got {cs['landing_gear'].evidence_status}"
        results.append(("case10_component_summaries_sync", True, "PASS"))
    except Exception as exc:
        results.append(("case10_component_summaries_sync", False, f"FAIL: {exc}"))

    # ---- Case 11: suggested_investigative_check with unknown ID -> needs_review in rationale ----
    try:
        bc_json = _assessment_with_bad_check_id(pack)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(bc_json), runs_dir=tmp_runs)
        assert rl.assessment is not None
        assert rl.verification is not None
        # The unknown ID in the check should produce a downgraded ClaimCheck
        dc = [c for c in rl.verification.claim_checks if c.downgraded and "P_UNKNOWN" in c.evidence_id]
        assert len(dc) > 0, "expected downgraded check for unknown ID in suggested check"
        # The rationale should have been appended with needs_review
        sc = rl.assessment.suggested_investigative_checks[0]
        assert "[needs_review:" in sc.rationale, f"expected needs_review in rationale, got: {sc.rationale}"
        results.append(("case11_bad_check_id_flagged", True, "PASS"))
    except Exception as exc:
        results.append(("case11_bad_check_id_flagged", False, f"FAIL: {exc}"))

    # ---- Case 12: Empty pack with run_label "A" -> error and zero llm calls ----
    try:
        empty_pack = _make_empty_pack()
        fake_llm = _make_counting_llm(["SHOULD NOT BE CALLED"])
        rl = run_analysis(empty_pack, "A", llm=fake_llm, runs_dir=tmp_runs)
        assert call_count == 0, f"expected 0 llm calls, got {call_count}"
        assert rl.assessment is None, "assessment should be None"
        assert rl.verification is None, "verification should be None"
        assert rl.error == "no evidence available for this component", \
            f"expected 'no evidence available for this component', got '{rl.error}'"
        results.append(("case12_empty_pack_non_zero_error", True, "PASS"))
    except Exception as exc:
        results.append(("case12_empty_pack_non_zero_error", False, f"FAIL: {exc}"))

    # ---- Case 13: Fake llm receives thinking and max_tokens values ----
    try:
        captured_kwargs: list[dict[str, Any]] = []
        valid_json = _valid_assessment_json(pack)

        def _capturing_llm(**kwargs: Any) -> LLMResult:
            captured_kwargs.append(kwargs)
            return LLMResult(
                text=valid_json,
                input_tokens=10,
                output_tokens=50,
                latency_s=0.5,
                tokens_per_s=100.0,
                model_name="test-model",
            )

        rl = run_analysis(
            pack,
            "B",
            temperature=0.2,
            thinking=True,
            max_tokens=2048,
            llm=_capturing_llm,
            runs_dir=tmp_runs,
        )
        assert len(captured_kwargs) == 1, f"expected 1 call, got {len(captured_kwargs)}"
        assert captured_kwargs[0].get("thinking") is True, \
            f"expected thinking=True, got {captured_kwargs[0].get('thinking')}"
        assert captured_kwargs[0].get("max_tokens") == 2048, \
            f"expected max_tokens=2048, got {captured_kwargs[0].get('max_tokens')}"
        assert rl.config.other.get("max_tokens") == 2048, \
            f"expected RunConfig.other['max_tokens']==2048, got {rl.config.other}"
        assert rl.config.thinking is True, f"expected RunConfig.thinking==True, got {rl.config.thinking}"
        results.append(("case13_llm_receives_thinking_and_max_tokens", True, "PASS"))
    except Exception as exc:
        results.append(("case13_llm_receives_thinking_and_max_tokens", False, f"FAIL: {exc}"))

    # ---- Case 14: wrapped output is unwrapped ----
    try:
        raw_dict = json.loads(_valid_assessment_json(pack))
        wrapped_json = json.dumps({"assessment": raw_dict})
        rl = run_analysis(pack, "A", llm=_make_fake_llm(wrapped_json), runs_dir=tmp_runs)
        assert rl.assessment is not None, "assessment should not be None when unwrapped"
        assert rl.assessment.brief_summary == "Mechanical failure in downlock assembly."
        assert rl.assessment.case_id == pack.case_id, f"expected case_id={pack.case_id}, got {rl.assessment.case_id}"
        assert rl.assessment.selected_component == pack.component_id, f"expected selected_component={pack.component_id}, got {rl.assessment.selected_component}"
        assert rl.error is None, f"expected no error, got {rl.error}"
        assert rl.verification is not None and rl.verification.status == "verified"
        results.append(("case14_wrapped_unwrapped", True, "PASS"))
    except Exception as exc:
        results.append(("case14_wrapped_unwrapped", False, f"FAIL: {exc}"))

    # ---- Case 15: missing case_id is filled from the pack ----
    try:
        raw_dict = json.loads(_valid_assessment_json(pack))
        del raw_dict["case_id"]
        raw_dict["selected_component"] = ""
        no_case_id_json = json.dumps(raw_dict)
        rl = run_analysis(pack, "A", llm=_make_fake_llm(no_case_id_json), runs_dir=tmp_runs)
        assert rl.assessment is not None, "assessment should not be None"
        assert rl.assessment.case_id == pack.case_id, f"expected case_id={pack.case_id}, got {rl.assessment.case_id}"
        assert rl.assessment.selected_component == pack.component_id, f"expected selected_component={pack.component_id}, got {rl.assessment.selected_component}"
        assert rl.error is None, f"expected no error, got {rl.error}"
        results.append(("case15_missing_case_id_filled", True, "PASS"))
    except Exception as exc:
        results.append(("case15_missing_case_id_filled", False, f"FAIL: {exc}"))

    # ---- Case 16: model-written component_summaries (list or dict) is discarded ----
    try:
        # Subcase 16a: component_summaries is a list
        raw_list = json.loads(_valid_assessment_json(pack))
        raw_list["component_summaries"] = [
            {"component": "landing_gear", "status": "bogus"},
            {"component": "engine", "status": "bogus"},
        ]
        list_cs_json = json.dumps(raw_list)
        rl_list = run_analysis(pack, "A", llm=_make_fake_llm(list_cs_json), runs_dir=tmp_runs)
        assert rl_list.assessment is not None, "assessment should not be None when component_summaries is list"
        assert list(rl_list.assessment.component_summaries.keys()) == [pack.component_id]
        assert rl_list.assessment.component_summaries[pack.component_id].passage_count == len(pack.passages)
        assert rl_list.error is None

        # Subcase 16b: component_summaries is a dict with bogus keys and counts
        raw_dict = json.loads(_valid_assessment_json(pack))
        raw_dict["component_summaries"] = {
            "bogus_engine": {"evidence_status": "insufficient", "passage_count": 999},
            pack.component_id: {"evidence_status": "insufficient", "passage_count": 1234},
        }
        dict_cs_json = json.dumps(raw_dict)
        rl_dict = run_analysis(pack, "A", llm=_make_fake_llm(dict_cs_json), runs_dir=tmp_runs)
        assert rl_dict.assessment is not None, "assessment should not be None when component_summaries is dict"
        assert list(rl_dict.assessment.component_summaries.keys()) == [pack.component_id]
        assert rl_dict.assessment.component_summaries[pack.component_id].passage_count == len(pack.passages)
        assert "bogus_engine" not in rl_dict.assessment.component_summaries
        assert rl_dict.error is None

        results.append(("case16_component_summaries_discarded", True, "PASS"))
    except Exception as exc:
        results.append(("case16_component_summaries_discarded", False, f"FAIL: {exc}"))

    # ---- Case 17: wrong-field-name output still FAILS (we do not guess renames) ----
    try:
        raw_bad = json.loads(_valid_assessment_json(pack))
        del raw_bad["brief_summary"]
        raw_bad["summary_text"] = "This is using a wrong field name."
        bad_field_json = json.dumps(raw_bad)
        fake_llm = _make_counting_llm([bad_field_json, bad_field_json])
        rl = run_analysis(pack, "A", llm=fake_llm, runs_dir=tmp_runs)
        assert rl.assessment is None, "assessment should be None when field name is wrong"
        assert rl.error is not None, "expected error in RunLog"
        assert rl.verification is not None and rl.verification.status == "failed"
        assert "brief_summary" in rl.error or "summary_text" in rl.error, f"expected field error in rl.error, got: {rl.error}"
        results.append(("case17_wrong_field_name_fails", True, "PASS"))
    except Exception as exc:
        results.append(("case17_wrong_field_name_fails", False, f"FAIL: {exc}"))

    # ---- Print summary ----
    all_passed = True
    for name, passed, msg in results:
        print(f"  {name}: {msg}")
        if not passed:
            all_passed = False

    # Clean up temp dir
    import shutil
    shutil.rmtree(tmp_runs, ignore_errors=True)

    if all_passed:
        print("PASS")
    else:
        print("FAIL")
        sys.exit(1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        print("Usage: python -m model.harness --selftest")

# ============================================================
# COMPONENT: C6 - Deterministic Verifier
# PURPOSE: Verify every citation in an Assessment against the EvidencePack (no model calls).
# INPUT: Assessment (parsed model output), EvidencePack (ground-truth passages).
# OUTPUT: Verification object; optionally a downgraded Assessment with resolved references.
# CALLED BY: model/harness.py      CALLS: common/models.py, common/trace.py
# IF IT BREAKS, CHECK: Assessment schema changes, whitespace-normalization edge cases, missing pack passages.
# ============================================================

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Optional

from common.models import (
    Assessment,
    ClaimCheck,
    EvidencePack,
    EvidencePassage,
    Verification,
)
from common.trace import ComponentError, trace

# Minimum number of characters (after normalization) for a quote to be considered valid.
MIN_QUOTE_CHARS: int = 20

# Normalization maps and patterns for robust PDF artifact handling
_CURLY_QUOTES_MAP = {
    "\u2018": "'",  # ‘ left single quotation mark
    "\u2019": "'",  # ’ right single quotation mark
    "\u201a": "'",  # ‚ single low-9 quotation mark
    "\u201b": "'",  # ‛ single high-reversed-9 quotation mark
    "\u201c": '"',  # “ left double quotation mark
    "\u201d": '"',  # ” right double quotation mark
    "\u201e": '"',  # „ double low-9 quotation mark
    "\u201f": '"',  # ‟ double high-reversed-9 quotation mark
    "\u2032": "'",  # ′ prime
    "\u2035": "'",  # ‵ reversed prime
    "\u2033": '"',  # ″ double prime
    "\u2036": '"',  # ‶ reversed double prime
}
_CURLY_QUOTES_TRANSLATION = str.maketrans(_CURLY_QUOTES_MAP)

_LIGATURE_MAP = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "st",
    "\ufb06": "st",
    "\u00e6": "ae",
    "\u00c6": "ae",
    "\u0153": "oe",
    "\u0152": "oe",
    "\u0133": "ij",
    "\u0132": "ij",
}

_NON_BREAKING_SPACES = ("\u00a0", "\u202f", "\u2007", "\ufeff")

_HYPHEN_DASH_PATTERN = re.compile(
    r"\s*[-\u2010-\u2015\u2212\ufe58\ufe63\uff0d]+(?:\s*[-\u2010-\u2015\u2212\ufe58\ufe63\uff0d]+)*\s*"
)


def _normalize(text: str) -> str:
    """IN: Raw string potentially containing PDF artifacts, ligatures, hyphens, or curly quotes.
    OUT: Normalized lowercase string with ligatures expanded, quotes straightened, hyphens removed, and whitespace collapsed.
    FAILS WITH: Never raises."""
    # STEP 1: Map curly quotes to straight | takes: text | gives: text with straight quotes
    text = text.translate(_CURLY_QUOTES_TRANSLATION)

    # STEP 2: Expand typographic ligatures | takes: text | gives: text with ligatures expanded
    for lig, repl in _LIGATURE_MAP.items():
        text = text.replace(lig, repl)

    # STEP 3: Replace non-breaking spaces with standard space | takes: text | gives: text with standard spaces
    for nbsp in _NON_BREAKING_SPACES:
        text = text.replace(nbsp, " ")

    # STEP 4: Remove soft hyphens | takes: text | gives: text with soft hyphens removed
    text = re.sub(r"\u00ad\s*", "", text)

    # STEP 5: Remove every hyphen/dash character together with surrounding whitespace | takes: text | gives: text without hyphens/dashes
    text = _HYPHEN_DASH_PATTERN.sub("", text)

    # STEP 6: Lowercase and collapse whitespace | takes: text | gives: normalized string
    return re.sub(r"\s+", " ", text.lower()).strip()


def _build_passage_map(pack: EvidencePack) -> dict[str, EvidencePassage]:
    """IN: EvidencePack.
    OUT: Dict mapping passage_id -> EvidencePassage.
    FAILS WITH: Never raises."""
    # STEP 1: Index passages by passage_id | takes: pack.passages | gives: lookup dict
    return {p.passage_id: p for p in pack.passages}


def verify_assessment(assessment: Assessment, pack: EvidencePack) -> Verification:
    """IN: Parsed Assessment, validated EvidencePack.
    OUT: Verification with claim_checks and overall status.
    FAILS WITH: ComponentError only on programming errors (not on verification failures)."""
    # STEP 1: Build passage lookup | takes: pack | gives: passage_map
    passage_map = _build_passage_map(pack)
    checks: list[ClaimCheck] = []
    any_failed = False

    # STEP 2: Check hypothesis citations | takes: assessment.hypotheses | gives: ClaimCheck entries
    for hyp in assessment.hypotheses:
        has_any_citation = bool(hyp.supporting_evidence) or bool(hyp.contradicting_evidence)

        # STEP 2a: Flag hypothesis with no citations at all | takes: hyp | gives: ClaimCheck
        if not has_any_citation:
            chk = ClaimCheck(
                claim_ref=f"{hyp.id}.no_citation",
                evidence_id="",
                id_exists=False,
                quote_found=False,
                downgraded=True,
                reason="no citation",
            )
            checks.append(chk)
            any_failed = True

        for cite in hyp.supporting_evidence:
            chk = _check_citation(f"{hyp.id}.supporting.{cite.evidence_id}", cite.evidence_id, cite.quote, passage_map)
            checks.append(chk)
            if chk.downgraded:
                any_failed = True

        # STEP 3: Check hypothesis contradicting_evidence citations | takes: hyp.contradicting_evidence | gives: ClaimCheck entries
        for cite in hyp.contradicting_evidence:
            chk = _check_citation(f"{hyp.id}.contradicting.{cite.evidence_id}", cite.evidence_id, cite.quote, passage_map)
            checks.append(chk)
            if chk.downgraded:
                any_failed = True

    # STEP 4: Check documented_findings citations | takes: assessment.documented_findings | gives: ClaimCheck entries
    for idx, finding in enumerate(assessment.documented_findings):
        chk = _check_citation(f"finding[{idx}].{finding.evidence_id}", finding.evidence_id, finding.quote, passage_map)
        checks.append(chk)
        if chk.downgraded:
            any_failed = True

    # STEP 5: Check suggested_investigative_checks evidence_ids exist | takes: assessment.suggested_investigative_checks | gives: ClaimCheck entries
    for idx, sc in enumerate(assessment.suggested_investigative_checks):
        for eid in sc.evidence_ids:
            id_exists = eid in passage_map
            downgraded = not id_exists
            reason = None if id_exists else f"evidence_id '{eid}' not found in pack"
            chk = ClaimCheck(
                claim_ref=f"check[{idx}].evidence_ids.{eid}",
                evidence_id=eid,
                id_exists=id_exists,
                quote_found=True,  # no quote to verify for checks
                downgraded=downgraded,
                reason=reason,
            )
            checks.append(chk)
            if downgraded:
                any_failed = True

    # STEP 6: Determine overall status | takes: checks | gives: status string
    if not checks:
        # No citations at all in the entire assessment -> failed
        status = "failed"
    elif any_failed:
        status = "partial"
    else:
        status = "verified"

    verification = Verification(status=status, claim_checks=checks, retries_used=0)
    trace(
        "C6", "Verification complete",
        status=verification.status,
        total_checks=len(checks),
        failed_checks=sum(1 for c in checks if c.downgraded),
    )
    return verification


def _check_citation(
    claim_ref: str,
    evidence_id: str,
    quote: str,
    passage_map: dict[str, EvidencePassage],
) -> ClaimCheck:
    """IN: claim_ref label, evidence_id, verbatim quote, passage lookup dict.
    OUT: ClaimCheck with id_exists, quote_found, downgraded, and reason.
    FAILS WITH: Never raises."""
    # STEP 1: Check evidence_id existence | takes: evidence_id, passage_map | gives: id_exists bool
    id_exists = evidence_id in passage_map

    # STEP 2: Normalize quote and measure minimum length after normalization | takes: quote | gives: quote_norm, validity
    quote_norm = _normalize(quote)

    if not quote_norm:
        # Empty or whitespace-only quote
        return ClaimCheck(
            claim_ref=claim_ref,
            evidence_id=evidence_id,
            id_exists=id_exists,
            quote_found=False,
            downgraded=True,
            reason="quote empty/too short",
        )

    if len(quote_norm) < MIN_QUOTE_CHARS:
        # Quote too short to be meaningful
        return ClaimCheck(
            claim_ref=claim_ref,
            evidence_id=evidence_id,
            id_exists=id_exists,
            quote_found=False,
            downgraded=True,
            reason="quote empty/too short",
        )

    # STEP 3: Check evidence_id exists before substring match | takes: id_exists | gives: early return or continue
    if not id_exists:
        return ClaimCheck(
            claim_ref=claim_ref,
            evidence_id=evidence_id,
            id_exists=False,
            quote_found=False,
            downgraded=True,
            reason=f"evidence_id '{evidence_id}' not found in pack",
        )

    # STEP 4: Check quote substring match (normalized) | takes: quote_norm, passage text | gives: quote_found bool
    passage_text_norm = _normalize(passage_map[evidence_id].text)
    quote_found = quote_norm in passage_text_norm

    # STEP 5: Determine downgrade | takes: quote_found | gives: downgraded, reason
    downgraded = False
    reason: Optional[str] = None
    if not quote_found:
        downgraded = True
        reason = f"quote not found in passage '{evidence_id}'"

    return ClaimCheck(
        claim_ref=claim_ref,
        evidence_id=evidence_id,
        id_exists=id_exists,
        quote_found=quote_found,
        downgraded=downgraded,
        reason=reason,
    )


def apply_downgrades(assessment: Assessment, verification: Verification) -> Assessment:
    """IN: Assessment and its Verification result.
    OUT: A copy of the Assessment with failed items flagged 'needs_review' and evidence_status lowered if needed.
    FAILS WITH: Never raises (operates on copies)."""
    # STEP 1: Deep-copy assessment to avoid mutation | takes: assessment | gives: mutable copy
    a = assessment.model_copy(deep=True)

    # Build a set of (claim_ref -> reason) for quick lookup
    downgraded_ids: dict[str, str] = {}
    for chk in verification.claim_checks:
        if chk.downgraded and chk.reason:
            downgraded_ids[chk.claim_ref] = chk.reason

    any_downgraded = False

    # STEP 2: Downgrade hypothesis statuses when their citations fail | takes: a.hypotheses, downgraded_ids | gives: modified hypotheses
    for hyp in a.hypotheses:
        hyp_failed = False
        # Check the "no citation" flag
        no_cite_key = f"{hyp.id}.no_citation"
        if no_cite_key in downgraded_ids:
            hyp_failed = True
        for cite in hyp.supporting_evidence:
            ref_key = f"{hyp.id}.supporting.{cite.evidence_id}"
            if ref_key in downgraded_ids:
                hyp_failed = True
        for cite in hyp.contradicting_evidence:
            ref_key = f"{hyp.id}.contradicting.{cite.evidence_id}"
            if ref_key in downgraded_ids:
                hyp_failed = True
        if hyp_failed:
            hyp.status = "unresolved"  # type: ignore[assignment]
            hyp.limitations.append("needs_review: one or more citations failed verification")
            any_downgraded = True

    # STEP 3: Flag documented findings whose citations fail | takes: a.documented_findings | gives: flagged findings
    for idx, finding in enumerate(a.documented_findings):
        ref_key = f"finding[{idx}].{finding.evidence_id}"
        if ref_key in downgraded_ids:
            reason = downgraded_ids[ref_key]
            finding.statement = f"[needs_review: {reason}] {finding.statement}"
            any_downgraded = True

    # STEP 4: Flag suggested_investigative_checks that cite unknown IDs | takes: a.suggested_investigative_checks, downgraded_ids | gives: flagged checks
    for idx, sc in enumerate(a.suggested_investigative_checks):
        for eid in sc.evidence_ids:
            ref_key = f"check[{idx}].evidence_ids.{eid}"
            if ref_key in downgraded_ids:
                reason = downgraded_ids[ref_key]
                sc.rationale = f"{sc.rationale} [needs_review: {reason}]"
                any_downgraded = True

    # STEP 5: Lower overall evidence_status if downgrades occurred | takes: any_downgraded flag | gives: adjusted status
    if any_downgraded and a.evidence_status == "supported":
        a.evidence_status = "mixed"
    elif any_downgraded and a.evidence_status == "mixed":
        a.evidence_status = "insufficient"

    trace("C6", "Downgrades applied", any_downgraded=any_downgraded, evidence_status=a.evidence_status)
    return a


def resolve_references(assessment: Assessment, pack: EvidencePack) -> Assessment:
    """IN: Assessment and EvidencePack.
    OUT: Assessment copy with component_summaries trimmed to pack's component and passage_count set from the pack.
    FAILS WITH: Never raises."""
    # STEP 1: Deep-copy and set the pack's component passage_count from the pack (authoritative).
    # Remove every other component key — the model only saw evidence for one component. | takes: assessment, pack | gives: corrected assessment
    a = assessment.model_copy(deep=True)

    from common.models import ComponentSummary
    # Ensure the pack's component exists in summaries with the correct passage_count
    if pack.component_id in a.component_summaries:
        a.component_summaries[pack.component_id].passage_count = len(pack.passages)
        a.component_summaries[pack.component_id].evidence_status = a.evidence_status
    else:
        a.component_summaries[pack.component_id] = ComponentSummary(
            evidence_status=a.evidence_status,
            passage_count=len(pack.passages),
        )

    # Remove every key that is not the pack's component
    keys_to_remove = [k for k in a.component_summaries if k != pack.component_id]
    for k in keys_to_remove:
        del a.component_summaries[k]

    trace("C6", "References resolved from pack", component=pack.component_id, passage_count=len(pack.passages))
    return a


def run_selftest() -> None:
    """IN: None (invoked via --selftest command line flag).
    OUT: None (prints test results and PASS/FAIL to stdout).
    FAILS WITH: SystemExit on test assertion failure or unhandled exception."""
    results: list[tuple[str, bool, str]] = []

    # STEP 1: Load fixtures from contracts/fixtures | takes: fixture JSON paths | gives: assessment, pack
    fixture_dir = Path(__file__).resolve().parent.parent / "contracts" / "fixtures"
    pack_file = fixture_dir / "pack.json"
    assessment_file = fixture_dir / "assessment.json"

    if not pack_file.exists() or not assessment_file.exists():
        print(f"FAIL: Fixture files missing in {fixture_dir}")
        sys.exit(1)

    try:
        pack = EvidencePack.model_validate_json(pack_file.read_text(encoding="utf-8"))
        assessment = Assessment.model_validate_json(assessment_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"FAIL: Failed to parse fixture models: {exc}")
        sys.exit(1)

    # STEP 2: Verify baseline fixture assessment passes | takes: assessment, pack | gives: verification status
    try:
        baseline_v = verify_assessment(assessment, pack)
        assert baseline_v.status == "verified", f"expected verified, got {baseline_v.status}"
        assert not any(c.downgraded for c in baseline_v.claim_checks), "expected 0 downgraded checks on baseline"
        downgraded_a = apply_downgrades(assessment, baseline_v)
        assert downgraded_a.evidence_status == assessment.evidence_status
        resolved_a = resolve_references(assessment, pack)
        assert pack.component_id in resolved_a.component_summaries
        results.append(("case1_fixtures_baseline", True, "PASS"))
    except Exception as exc:
        results.append(("case1_fixtures_baseline", False, f"FAIL: {exc}"))

    # STEP 3: Verify normalization unit cases | takes: raw strings with PDF artifacts | gives: normalized strings
    try:
        assert _normalize("fail-\nure") == "failure", f"fail-\\nure failed: {_normalize('fail-\\nure')}"
        assert _normalize("failure") == "failure", f"failure failed: {_normalize('failure')}"
        assert _normalize("fail - ure") == "failure", f"fail - ure failed: {_normalize('fail - ure')}"
        assert _normalize("“quoted text” and ‘single’") == '"quoted text" and \'single\'', "curly quotes normalization failed"
        assert _normalize("ﬁnd ﬂight œuvr æther") == "find flight oeuvr aether", "ligature expansion failed"
        assert _normalize("non\u00a0breaking\u202fspace") == "non breaking space", "nbsp normalization failed"
        assert _normalize("soft\u00adhyphen") == "softhyphen", "soft hyphen removal failed"
        results.append(("case2_normalization_units", True, "PASS"))
    except Exception as exc:
        results.append(("case2_normalization_units", False, f"FAIL: {exc}"))

    # STEP 4: Test a straight-quote quote matches a curly-quote passage | takes: quote with straight quotes, passage with curly quotes | gives: quote_found True
    try:
        p_curly = EvidencePassage(
            passage_id="P_CURLY_01",
            document_title="Fixture Doc",
            page_number=1,
            source_url="http://fixture.test/doc.pdf",
            evidence_role="observation",
            text="Visual examination confirmed “extensive fatigue striations” along the ‘shear’ plane.",
        )
        straight_quote = 'Visual examination confirmed "extensive fatigue striations" along the \'shear\' plane.'
        chk = _check_citation("test.curly_quote", p_curly.passage_id, straight_quote, {p_curly.passage_id: p_curly})
        assert chk.id_exists is True, "expected id_exists True"
        assert chk.quote_found is True, f"expected quote_found True, got {chk.quote_found} (reason: {chk.reason})"
        assert chk.downgraded is False, "expected downgraded False"
        results.append(("case3_straight_quote_matches_curly_passage", True, "PASS"))
    except Exception as exc:
        results.append(("case3_straight_quote_matches_curly_passage", False, f"FAIL: {exc}"))

    # STEP 5: Test a hyphen-linebreak passage matches the unbroken quote | takes: unbroken quote, passage with hyphen-linebreak | gives: quote_found True
    try:
        p_hyphen = EvidencePassage(
            passage_id="P_HYPHEN_01",
            document_title="Fixture Doc",
            page_number=2,
            source_url="http://fixture.test/doc.pdf",
            evidence_role="observation",
            text="Post-flight inspection revealed that the main landing gear downlock pin had fail-\nure along the shear plane.",
        )
        unbroken_quote = "Post-flight inspection revealed that the main landing gear downlock pin had failure along the shear plane."
        chk = _check_citation("test.hyphen_linebreak", p_hyphen.passage_id, unbroken_quote, {p_hyphen.passage_id: p_hyphen})
        assert chk.id_exists is True, "expected id_exists True"
        assert chk.quote_found is True, f"expected quote_found True, got {chk.quote_found} (reason: {chk.reason})"
        assert chk.downgraded is False, "expected downgraded False"

        # Also verify hyphen with space e.g. "fail - ure" matches unbroken quote
        p_hyphen_space = EvidencePassage(
            passage_id="P_HYPHEN_02",
            document_title="Fixture Doc",
            page_number=2,
            source_url="http://fixture.test/doc.pdf",
            evidence_role="observation",
            text="Post-flight inspection revealed that the main landing gear downlock pin had fail - ure along the shear plane.",
        )
        chk_space = _check_citation("test.hyphen_space", p_hyphen_space.passage_id, unbroken_quote, {p_hyphen_space.passage_id: p_hyphen_space})
        assert chk_space.quote_found is True, f"expected quote_found True for fail - ure, got {chk_space.quote_found}"
        assert chk_space.downgraded is False

        results.append(("case4_hyphen_linebreak_matches_unbroken_quote", True, "PASS"))
    except Exception as exc:
        results.append(("case4_hyphen_linebreak_matches_unbroken_quote", False, f"FAIL: {exc}"))

    # STEP 6: Test a changed word still fails | takes: quote with altered word | gives: quote_found False, downgraded True
    try:
        p_target = EvidencePassage(
            passage_id="P_TARGET_01",
            document_title="Fixture Doc",
            page_number=3,
            source_url="http://fixture.test/doc.pdf",
            evidence_role="observation",
            text="Post-flight inspection revealed that the main landing gear downlock pin had fractured along the shear plane.",
        )
        changed_quote = "Post-flight inspection revealed that the main landing gear downlock pin had exploded along the shear plane."
        chk = _check_citation("test.changed_word", p_target.passage_id, changed_quote, {p_target.passage_id: p_target})
        assert chk.id_exists is True, "expected id_exists True"
        assert chk.quote_found is False, "expected quote_found False for changed word"
        assert chk.downgraded is True, "expected downgraded True for changed word"
        assert chk.reason == f"quote not found in passage '{p_target.passage_id}'"
        results.append(("case5_changed_word_still_fails", True, "PASS"))
    except Exception as exc:
        results.append(("case5_changed_word_still_fails", False, f"FAIL: {exc}"))

    # STEP 7: Test minimum quote length measured AFTER normalization | takes: raw quote >= 20 chars but < 20 after norm | gives: downgraded True
    try:
        p_len = EvidencePassage(
            passage_id="P_LEN_01",
            document_title="Fixture Doc",
            page_number=4,
            source_url="http://fixture.test/doc.pdf",
            evidence_role="observation",
            text="abcdefghij extra padding passage content for testing quote length.",
        )
        # Raw quote length is 37 characters (>= 20), but after hyphen/space removal it is 'abcdefghij' (10 chars < 20)
        short_post_norm_quote = "a - b - c - d - e - f - g - h - i - j"
        assert len(short_post_norm_quote) >= MIN_QUOTE_CHARS
        chk = _check_citation("test.short_post_norm", p_len.passage_id, short_post_norm_quote, {p_len.passage_id: p_len})
        assert chk.quote_found is False, "expected quote_found False because post-norm length < MIN_QUOTE_CHARS"
        assert chk.downgraded is True, "expected downgraded True"
        assert chk.reason == "quote empty/too short"
        results.append(("case6_min_quote_length_after_normalization", True, "PASS"))
    except Exception as exc:
        results.append(("case6_min_quote_length_after_normalization", False, f"FAIL: {exc}"))

    # STEP 8: Report test results and final status | takes: results | gives: stdout summary and trace call
    all_passed = True
    for name, passed, msg in results:
        print(f"  {name}: {msg}")
        if not passed:
            all_passed = False

    trace("C6", "Verifier self-test complete", total=len(results), passed=sum(1 for _, p, _ in results if p))

    if all_passed:
        print("PASS")
    else:
        print("FAIL")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic Verifier operations and self-test")
    parser.add_argument("--selftest", action="store_true", help="Execute component self-test")
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
    else:
        print("Usage: python -m model.verifier --selftest")


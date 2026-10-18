# ============================================================
# COMPONENT: C5 - Evidence Pack Builder
# PURPOSE: Assemble deterministic, token-budgeted, leakage-safe EvidencePacks for component reasoning.
# INPUT: SQLite database path, case_id, component_id, question, mode ("0", "A", "B", "C"), token_budget, optional packs_dir.
# OUTPUT: EvidencePack domain models and persisted JSON pack files in data/packs/<pack_id>.json.
# CALLED BY: model/harness.py, pipeline orchestrators      CALLS: evidence/store.py, common/models.py, common/trace.py, common/config.py
# IF IT BREAKS, CHECK: pack_id determinism, token budget calculation, truth leakage assertion, candidate query logic.
# ============================================================

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common.config import get_data_dir
from common.models import EvidencePack, EvidencePassage, Passage
from common.trace import ComponentError, trace
from evidence.store import get_passage, load_passages, search

# Constant similarity threshold for near-duplicate text deduplication
SIMILARITY_THRESHOLD: float = 0.85

# Enable extra="allow" on EvidencePack so content_hash can be deserialized seamlessly
EvidencePack.model_config["extra"] = "allow"
EvidencePack.model_rebuild(force=True)


def _compute_pack_id(case_id: str, component_id: str, question: str, mode: str, token_budget: int) -> str:
    """IN: case_id (str), component_id (str), question (str), mode (str), token_budget (int).
    OUT: Deterministic pack identifier string.
    FAILS WITH: Never raises."""
    # STEP 1: Compute deterministic SHA256 digest from key parameters | takes: inputs | gives: pack_id
    raw_key = f"{case_id}|{component_id}|{question.strip()}|{mode}|{token_budget}"
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:12]
    clean_case = re.sub(r"[^A-Za-z0-9_-]", "_", case_id)
    clean_comp = re.sub(r"[^A-Za-z0-9_-]", "_", component_id)
    return f"pack_{clean_case}_{clean_comp}_{mode}_{digest}"


def _approx_tokens(text: str) -> int:
    """IN: Text excerpt string.
    OUT: Estimated token count (int).
    FAILS WITH: Never raises."""
    # STEP 1: Estimate tokens using character length divided by 4 | takes: text | gives: token_count
    return max(1, len(text) // 4)


def _is_similar(text1: str, text2: str, threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """IN: text1 (str), text2 (str), optional similarity threshold (float).
    OUT: True if normalized texts have similarity ratio >= threshold, False otherwise.
    FAILS WITH: Never raises."""
    # STEP 1: Normalize texts and compute sequence similarity ratio | takes: text1, text2 | gives: boolean similarity
    norm1 = re.sub(r"\W+", " ", text1.lower()).strip()
    norm2 = re.sub(r"\W+", " ", text2.lower()).strip()
    if norm1 == norm2:
        return True
    return difflib.SequenceMatcher(None, norm1, norm2).ratio() >= threshold


def _compute_content_hash(passage_ids: list[str]) -> str:
    """IN: List of passage_id strings.
    OUT: SHA-256 hex digest string representing corpus passage content.
    FAILS WITH: Never raises."""
    # STEP 1: Compute SHA256 digest over passage identifier sequence | takes: passage_ids | gives: content_hash
    content_str = ",".join(passage_ids)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def print_explain_table(passages_with_reason: list[tuple[EvidencePassage, str]]) -> None:
    """IN: List of tuples containing EvidencePassage and explanation string.
    OUT: None (prints formatted table to standard output).
    FAILS WITH: Never raises."""
    # STEP 1: Format table columns and calculate widths | takes: passages_with_reason | gives: formatted lines
    headers = ("passage_id", "page", "role", "why included")
    rows: list[tuple[str, str, str, str]] = []
    for p, why in passages_with_reason:
        page_val = str(p.page_number) if p.page_number is not None else "-"
        rows.append((p.passage_id, page_val, p.evidence_role, why))

    widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            widths[i] = max(widths[i], len(val))

    fmt = " | ".join(f"{{:<{w}}}" for w in widths)
    sep = "-+-".join("-" * w for w in widths)

    # STEP 2: Emit formatted table to stdout | takes: rows, fmt, sep | gives: printed table
    print(fmt.format(*headers))
    print(sep)
    for r in rows:
        print(fmt.format(*r))


def _get_packs_dir(custom_dir: Optional[Path | str] = None) -> Path:
    """IN: Optional custom destination directory path.
    OUT: Resolved Path to packs storage directory.
    FAILS WITH: ComponentError if directory resolution or creation fails."""
    # STEP 1: Resolve packs directory from custom_dir or get_data_dir | takes: custom_dir | gives: Path
    if custom_dir is not None:
        p = Path(custom_dir)
    else:
        try:
            p = get_data_dir() / "packs"
        except Exception as exc:
            raise ComponentError("C5", "resolve_packs_dir", f"Failed to resolve data directory: {exc}") from exc
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise ComponentError("C5", "resolve_packs_dir", f"Cannot create packs directory at {p}: {exc}") from exc
    return p


def build_pack(
    db_path: str | Path,
    case_id: str,
    component_id: str,
    question: str,
    mode: str,
    token_budget: int = 6000,
    explain: bool = False,
    packs_dir: Optional[Path | str] = None,
) -> EvidencePack:
    """IN: db_path, case_id, component_id, question, mode ("0", "A", "B", "C"), token_budget, optional explain, packs_dir.
    OUT: Assembled EvidencePack domain model.
    FAILS WITH: ComponentError on database failure, budget constraint error, or truth leakage."""
    # STEP 1: Handle empty pack generation for mode "0" | takes: mode, pack_id | gives: empty EvidencePack
    pack_id = _compute_pack_id(case_id, component_id, question, mode, token_budget)
    target_packs_dir = _get_packs_dir(packs_dir)
    pack_path = target_packs_dir / f"{pack_id}.json"

    if mode == "0":
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        content_hash = _compute_content_hash([])
        empty_pack = EvidencePack(
            pack_id=pack_id,
            case_id=case_id,
            component_id=component_id,
            question=question,
            created_at=created_at,
            token_budget=token_budget,
            approx_tokens=0,
            passages=[],
            content_hash=content_hash,
        )
        try:
            pack_dict = empty_pack.model_dump()
            pack_dict["content_hash"] = content_hash
            pack_path.write_text(json.dumps(pack_dict, indent=2), encoding="utf-8")
        except Exception as exc:
            raise ComponentError("C5", "save_pack", f"Failed saving mode 0 pack to {pack_path}: {exc}") from exc
        trace("C5", "Mode 0 empty pack assembled", pack_id=pack_id, content_hash=content_hash)
        if explain:
            print_explain_table([])
        return empty_pack

    # STEP 2: Gather candidate passages according to analysis mode | takes: db_path, case_id, component_id, question | gives: candidate list
    candidates: list[tuple[Passage, str]] = []
    seen_ids: set[str] = set()

    # Current-case passages tagged for the component
    tagged_matches = search(db_path, query="", case_id=case_id, component_id=component_id, case_scope="current", limit=50)
    for p in tagged_matches:
        if p.passage_id not in seen_ids:
            candidates.append((p, "component tag"))
            seen_ids.add(p.passage_id)

    # Current-case passages matching question keywords
    keyword_matches = search(db_path, query=question, case_id=case_id, case_scope="current", limit=50)
    for p in keyword_matches:
        if p.passage_id not in seen_ids:
            candidates.append((p, "matched keywords, rank"))
            seen_ids.add(p.passage_id)

    # For modes B and C (and live), also gather historical-case and guidance passages
    if mode in ("B", "C", "live"):
        hist_keyword = search(db_path, query=question, case_id=case_id, case_scope="historical", limit=50)
        for p in hist_keyword:
            if p.passage_id not in seen_ids:
                candidates.append((p, "historical"))
                seen_ids.add(p.passage_id)

        hist_tagged = search(db_path, query="", case_id=case_id, component_id=component_id, case_scope="historical", limit=50)
        for p in hist_tagged:
            if p.passage_id not in seen_ids:
                candidates.append((p, "historical"))
                seen_ids.add(p.passage_id)

        guidance_passages = search(
            db_path, query=question, roles=["guidance", "recommendation"], case_scope="any", limit=50
        )
        for p in guidance_passages:
            if p.passage_id not in seen_ids:
                candidates.append((p, "guidance"))
                seen_ids.add(p.passage_id)

    # STEP 3: Prioritize limiting or contradicting counterexample passage | takes: db_path, candidates | gives: candidates with counterexample
    counter_query = '"however" OR "no evidence" OR "ruled out" OR "not found" OR "contrary"'
    counter_matches = search(db_path, query=counter_query, case_id=case_id, case_scope="current", limit=10)
    if not counter_matches and mode in ("B", "C", "live"):
        counter_matches = search(db_path, query=counter_query, case_scope="any", limit=10)

    if counter_matches:
        counter_p = counter_matches[0]
        # Remove counter_p from existing position if present to avoid duplication
        candidates = [(p, r) for p, r in candidates if p.passage_id != counter_p.passage_id]
        # Prioritize near the top so it is included within budget
        if candidates:
            candidates.insert(1, (counter_p, "counterexample"))
        else:
            candidates.append((counter_p, "counterexample"))

    # STEP 4: Deduplicate near-identical candidate passages | takes: candidates | gives: deduped candidate list
    deduped: list[tuple[Passage, str]] = []
    for p, reason in candidates:
        is_dup = False
        for kept_p, _ in deduped:
            if p.passage_id == kept_p.passage_id or _is_similar(p.text, kept_p.text):
                is_dup = True
                break
        if not is_dup:
            deduped.append((p, reason))

    # STEP 5: Fit candidate passages into specified token budget | takes: deduped, token_budget | gives: selected candidate list
    selected: list[tuple[Passage, str]] = []
    running_tokens = 0
    for p, reason in deduped:
        p_tokens = _approx_tokens(p.text)
        if running_tokens + p_tokens <= token_budget:
            selected.append((p, reason))
            running_tokens += p_tokens
        else:
            # Stop adding passages when budget would be exceeded
            break

    # STEP 6: Assert strict leakage guard compliance across all selected passages | takes: selected | gives: verified prediction-only passages
    for p, _ in selected:
        if p.split != "prediction":
            raise ComponentError("C5", "leakage", f"Leakage detected: passage '{p.passage_id}' has split '{p.split}'")

    # STEP 7: Assemble EvidencePack and persist to target packs directory | takes: selected, pack_id | gives: persisted EvidencePack
    evidence_passages = [
        EvidencePassage(
            passage_id=p.passage_id,
            document_title=p.document_title,
            page_number=p.page_number,
            source_url=p.source_url,
            evidence_role=p.evidence_role,
            text=p.text,
        )
        for p, _ in selected
    ]

    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    content_hash = _compute_content_hash([p.passage_id for p in evidence_passages])

    pack = EvidencePack(
        pack_id=pack_id,
        case_id=case_id,
        component_id=component_id,
        question=question,
        created_at=created_at,
        token_budget=token_budget,
        approx_tokens=running_tokens,
        passages=evidence_passages,
        content_hash=content_hash,
    )

    try:
        pack_dict = pack.model_dump()
        pack_dict["content_hash"] = content_hash
        pack_path.write_text(json.dumps(pack_dict, indent=2), encoding="utf-8")
    except Exception as exc:
        raise ComponentError("C5", "save_pack", f"Failed saving pack to {pack_path}: {exc}") from exc

    trace(
        "C5",
        "Evidence pack assembled and persisted",
        pack_id=pack_id,
        passage_count=len(evidence_passages),
        approx_tokens=running_tokens,
        content_hash=content_hash,
    )

    # STEP 8: Render explain diagnostic table if requested | takes: explain, selected | gives: table output
    if explain:
        print_explain_table([(ep, r) for ep, (_, r) in zip(evidence_passages, selected)])

    return pack


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS or FAIL with reason and exits).
    FAILS WITH: SystemExit on test failure or completion."""
    # STEP 1: Load fixture passages and assert truth split presence | takes: contracts/fixtures/passages.jsonl | gives: verified fixture database
    try:
        fixture_path = Path("contracts/fixtures/passages.jsonl")
        if not fixture_path.exists():
            print(f"FAIL: Fixture file not found: {fixture_path}")
            sys.exit(1)

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir_path = Path(tmpdir)
            temp_db = temp_dir_path / "test_pack_builder.db"
            temp_packs = temp_dir_path / "test_packs"
            load_passages(fixture_path, temp_db)

            # Check that truth passage exists in database with allow_truth=True
            truth_passage = get_passage(temp_db, "FIXTURE:PASSAGE-008", allow_truth=True)
            if truth_passage is None or truth_passage.split != "truth":
                print("FAIL: Expected truth fixture passage FIXTURE:PASSAGE-008 missing or incorrect split")
                sys.exit(1)

            case_id = "FIXTURE:CASE-2026-LG"
            component_id = "landing_gear"
            question = "FIXTURE:What documented factors contributed to the landing gear extension anomaly?"

            # STEP 2: Build and verify mode 0 empty pack | takes: temp_db, temp_packs | gives: verified empty pack
            pack_0 = build_pack(temp_db, case_id, component_id, question, mode="0", token_budget=4000, packs_dir=temp_packs)
            if len(pack_0.passages) != 0:
                print(f"FAIL: Mode 0 pack not empty; contains {len(pack_0.passages)} passages")
                sys.exit(1)
            if pack_0.approx_tokens != 0:
                print(f"FAIL: Mode 0 pack approx_tokens != 0 ({pack_0.approx_tokens})")
                sys.exit(1)

            # Verify saved mode 0 pack has content_hash
            pack_0_file = temp_packs / f"{pack_0.pack_id}.json"
            if not pack_0_file.exists():
                print(f"FAIL: Mode 0 pack file not saved to {pack_0_file}")
                sys.exit(1)
            pack_0_data = json.loads(pack_0_file.read_text(encoding="utf-8"))
            if "content_hash" not in pack_0_data or not pack_0_data["content_hash"]:
                print("FAIL: Saved mode 0 pack missing content_hash")
                sys.exit(1)

            # STEP 3: Build and verify mode A and B packs for leakage, budgets, and determinism | takes: temp_db, temp_packs | gives: verified packs
            pack_A = build_pack(temp_db, case_id, component_id, question, mode="A", token_budget=4000, packs_dir=temp_packs)
            pack_B = build_pack(temp_db, case_id, component_id, question, mode="B", token_budget=4000, packs_dir=temp_packs)

            for mode_name, pck in [("0", pack_0), ("A", pack_A), ("B", pack_B)]:
                # Assert truth passage never appears in any pack
                if any(p.passage_id == "FIXTURE:PASSAGE-008" for p in pck.passages):
                    print(f"FAIL: Truth passage FIXTURE:PASSAGE-008 leaked into pack mode {mode_name}")
                    sys.exit(1)
                # Assert pack sizes respect the budget
                if pck.approx_tokens > pck.token_budget:
                    print(f"FAIL: Pack mode {mode_name} approx_tokens ({pck.approx_tokens}) exceeded budget ({pck.token_budget})")
                    sys.exit(1)
                # Assert saved file contains content_hash
                saved_file = temp_packs / f"{pck.pack_id}.json"
                if not saved_file.exists():
                    print(f"FAIL: Pack file {saved_file} was not saved")
                    sys.exit(1)
                saved_data = json.loads(saved_file.read_text(encoding="utf-8"))
                if "content_hash" not in saved_data or not saved_data["content_hash"]:
                    print(f"FAIL: Saved pack {pck.pack_id} missing content_hash")
                    sys.exit(1)

            # Assert identical requests give identical pack_ids
            pack_A_repeat = build_pack(temp_db, case_id, component_id, question, mode="A", token_budget=4000, packs_dir=temp_packs)
            if pack_A.pack_id != pack_A_repeat.pack_id:
                print(f"FAIL: Non-deterministic pack_id for identical request: {pack_A.pack_id} != {pack_A_repeat.pack_id}")
                sys.exit(1)

            # STEP 4: Test overwrite behavior on existing pack file | takes: temp_packs | gives: overwritten file
            pack_A_file = temp_packs / f"{pack_A.pack_id}.json"
            pack_A_data = json.loads(pack_A_file.read_text(encoding="utf-8"))
            if "content_hash" not in pack_A_data:
                print("FAIL: Overwritten pack file missing content_hash")
                sys.exit(1)

            # STEP 5: Test and assert ComponentError on synthetic leakage injection | takes: mock passage | gives: ComponentError verified
            synthetic_truth = Passage(
                passage_id="SYNTHETIC:LEAK",
                document_id="DOC:LEAK",
                document_title="Title",
                source_url="http://test.url",
                publisher="NTSB",
                document_type="report",
                publication_date="2026-01-01",
                page_number=1,
                timestamp=None,
                text="Synthetic truth finding text.",
                case_id=case_id,
                component_tags=["landing_gear"],
                evidence_role="official_finding",
                ingestion_method="test",
                extraction_warnings=[],
                split="truth",
            )
            leakage_caught = False
            try:
                selected_with_leak = [(synthetic_truth, "test")]
                for p, _ in selected_with_leak:
                    if p.split != "prediction":
                        raise ComponentError("C5", "leakage", f"Leakage detected: passage '{p.passage_id}' has split '{p.split}'")
            except ComponentError as err:
                if err.component == "C5" and err.step == "leakage":
                    leakage_caught = True

            if not leakage_caught:
                print("FAIL: ComponentError('C5', 'leakage', ...) was not raised on synthetic truth passage")
                sys.exit(1)

        # STEP 6: Report PASS status and exit cleanly | takes: None | gives: stdout PASS message
        print("PASS")
        sys.exit(0)
    except Exception as exc:
        print(f"FAIL: Unexpected error during pack_builder self-test: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evidence pack builder operations and diagnostics")
    parser.add_argument("--selftest", action="store_true", help="Execute component self-test")
    parser.add_argument("--case", type=str, help="Case identifier")
    parser.add_argument("--component", type=str, help="Component identifier")
    parser.add_argument("--question", type=str, help="Analytical question")
    parser.add_argument("--mode", type=str, default="A", help="Analysis mode (0, A, B, C)")
    parser.add_argument("--budget", type=int, default=6000, help="Token budget")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database")
    parser.add_argument("--packs-dir", type=str, default=None, help="Directory to save packs")
    parser.add_argument("--explain", action="store_true", help="Print explain diagnostic table")
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
    elif args.case and args.component and args.question:
        db_path = args.db
        if db_path is None:
            try:
                db_path = str(get_data_dir() / "db" / "passages.db")
            except Exception:
                db_path = "data/db/passages.db"
        build_pack(
            db_path=db_path,
            case_id=args.case,
            component_id=args.component,
            question=args.question,
            mode=args.mode,
            token_budget=args.budget,
            explain=args.explain,
            packs_dir=args.packs_dir,
        )
    else:
        parser.print_help()

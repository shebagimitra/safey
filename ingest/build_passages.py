# ============================================================
# COMPONENT: C4 - Passage Construction & Manifest Ingestion
# PURPOSE: Ingest PDF documents declared in manifest, segment text into bounded passages, tag components, and write passages.jsonl.
# INPUT: Manifest JSON file (data/manifest.json), max_chars, overlap_chars.
# OUTPUT: JSONL passage records (data/processed/passages.jsonl) and summary dictionary.
# CALLED BY: Pipeline orchestrators, CLI invocations      CALLS: ingest/local_pdf_pages.py, common/models.py, common/trace.py
# IF IT BREAKS, CHECK: Manifest schema validity, PDF file resolution relative to project root, passage splitting boundaries.
# ============================================================

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from common.models import Passage, validate_manifest, validate_passage
from common.trace import ComponentError, trace
from ingest.local_pdf_pages import _create_test_pdf_bytes, extract_pages

COMPONENT_KEYWORDS: dict[str, list[str]] = {
    "engine": [
        "engine",
        "turbine",
        "turbofan",
        "compressor",
        "fan blade",
        "thrust",
        "fadec",
        "egt",
        "cowling",
        "propeller",
        "combustor",
    ],
    "landing_gear": [
        "landing gear",
        "gear",
        "downlock",
        "uplock",
        "actuator",
        "strut",
        "oleo",
        "wheel",
        "brakes",
        "brake",
        "tire",
        "nosewheel",
    ],
    "hydraulic_system": [
        "hydraulic",
        "hydraulics",
        "hydraulic fluid",
        "skydrol",
        "reservoir",
        "actuator cylinder",
        "hydraulic line",
        "hydraulic pump",
        "hydraulic pressure",
    ],
    "wing_flaps": [
        "flap",
        "flaps",
        "slat",
        "slats",
        "wing flap",
        "trailing edge",
        "leading edge",
        "flap track",
        "fairing",
    ],
    "fuel_system": [
        "fuel",
        "fuel tank",
        "fuel pump",
        "fuel line",
        "kerosene",
        "fuel valve",
        "crossfeed",
        "fuel pressure",
        "fuel filter",
        "avgas",
    ],
}

ALLOWED_EVIDENCE_ROLES: set[str] = {
    "observation",
    "official_finding",
    "recommendation",
    "guidance",
    "corrective_action",
}


def tag_components(text: str) -> list[str]:
    """IN: text (str) passage content.
    OUT: list[str] matched component names from COMPONENT_KEYWORDS.
    FAILS WITH: Never fails."""
    # STEP 1: Search text for component keywords using word boundaries | takes: text, COMPONENT_KEYWORDS | gives: assigned_tags
    assigned: list[str] = []
    text_lower = text.lower()
    for component, keywords in COMPONENT_KEYWORDS.items():
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, text_lower):
                if component not in assigned:
                    assigned.append(component)
                break
    return assigned


def _split_page_into_passages(text: str, max_chars: int = 1500, overlap_chars: int = 150) -> list[str]:
    """IN: text (str), max_chars (int), overlap_chars (int).
    OUT: list[str] text segments bounded by max_chars at paragraph/sentence boundaries.
    FAILS WITH: ComponentError on invalid segmentation parameters."""
    # STEP 1: Validate input parameters and handle empty text | takes: text, max_chars, overlap_chars | gives: clean_text
    if max_chars <= 0:
        raise ComponentError("C4", "split_passages", f"max_chars must be positive, got {max_chars}")
    clean_text = text.strip()
    if not clean_text:
        return []
    if len(clean_text) <= max_chars:
        return [clean_text]

    # STEP 2: Deconstruct page text into paragraph and sentence atomic pieces | takes: clean_text | gives: pieces
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", clean_text) if p.strip()]
    pieces: list[str] = []
    for para in paragraphs:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", para) if s.strip()]
        for sent in sentences:
            if len(sent) <= max_chars:
                pieces.append(sent)
            else:
                words = sent.split()
                cur_chunk: list[str] = []
                cur_len = 0
                for w in words:
                    w_len = len(w) + (1 if cur_chunk else 0)
                    if cur_len + w_len <= max_chars:
                        cur_chunk.append(w)
                        cur_len += w_len
                    else:
                        if cur_chunk:
                            pieces.append(" ".join(cur_chunk))
                        cur_chunk = [w]
                        cur_len = len(w)
                if cur_chunk:
                    pieces.append(" ".join(cur_chunk))

    if not pieces:
        return []

    # STEP 3: Assemble pieces into overlapping bounded passages | takes: pieces, max_chars, overlap_chars | gives: passages
    passages: list[str] = []
    i = 0
    while i < len(pieces):
        chunk_pieces: list[str] = []
        chunk_len = 0
        j = i
        while j < len(pieces):
            piece = pieces[j]
            added_len = len(piece) + (1 if chunk_pieces else 0)
            if chunk_len + added_len <= max_chars:
                chunk_pieces.append(piece)
                chunk_len += added_len
                j += 1
            else:
                break

        if not chunk_pieces:
            chunk_pieces.append(pieces[i][:max_chars])
            j = i + 1

        passage_str = " ".join(chunk_pieces).strip()
        if passage_str:
            passages.append(passage_str)

        if j >= len(pieces):
            break

        if overlap_chars > 0:
            overlap_len = 0
            back_j = j - 1
            while back_j > i:
                piece_len = len(pieces[back_j]) + 1
                if overlap_len + piece_len <= overlap_chars:
                    overlap_len += piece_len
                    back_j -= 1
                else:
                    break
            i = max(i + 1, back_j + 1)
        else:
            i = j

    return passages


def _resolve_doc_path(file_path: str, manifest_dir: Path) -> Path:
    """IN: file_path (str) from manifest, manifest_dir (Path) directory containing manifest.
    OUT: Path pointing to existing PDF document file.
    FAILS WITH: ComponentError if file cannot be located on disk."""
    # STEP 1: Attempt direct and relative path resolutions | takes: file_path, manifest_dir | gives: resolved Path
    raw_path = Path(file_path)
    if raw_path.is_absolute() and raw_path.exists():
        return raw_path

    # Candidate 1: relative to manifest file directory
    c1 = manifest_dir / raw_path
    if c1.exists():
        return c1.resolve()

    # Candidate 2: relative to project root
    project_root = Path(__file__).resolve().parent.parent
    c2 = project_root / raw_path
    if c2.exists():
        return c2.resolve()

    # Candidate 3: relative to configured DATA_DIR
    try:
        from common.config import get_data_dir
        c3 = get_data_dir() / raw_path
        if c3.exists():
            return c3.resolve()
        c4 = get_data_dir() / raw_path.name
        if c4.exists():
            return c4.resolve()
    except Exception:
        pass

    raise ComponentError("C4", "resolve_doc_path", f"Unable to locate document file on disk: {file_path}")


def print_report(doc_reports: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    """IN: doc_reports (list of per-document summary dicts), summary (overall summary dict).
    OUT: None (emits formatted report table to stdout).
    FAILS WITH: Never fails."""
    # STEP 1: Format and print table header | takes: None | gives: stdout header
    print("=" * 80)
    print(f"{'Document ID':<25} {'Pages':<8} {'Passages':<10} {'Warning Pages'}")
    print("-" * 80)

    # STEP 2: Iterate documents and print per-document rows | takes: doc_reports | gives: stdout rows
    for r in doc_reports:
        warning_str = ", ".join(str(p) for p in r["warning_pages"]) if r["warning_pages"] else "None"
        print(f"{r['document_id']:<25} {r['pages']:<8} {r['passages']:<10} {warning_str}")

    print("-" * 80)
    print(
        f"Total: {summary['documents']} documents, "
        f"{summary['pages']} pages, "
        f"{summary['passages']} passages, "
        f"{summary['pages_with_warnings']} warning pages"
    )
    print("=" * 80)


def build_passages(
    manifest_path: str | Path,
    out_path: str | Path,
    max_chars: int = 1500,
    overlap_chars: int = 150,
    tag_components_enabled: bool = True,
    report: bool = False,
) -> dict[str, Any]:
    """IN: manifest_path (str or Path), out_path (str or Path), max_chars (int), overlap_chars (int).
    OUT: dict summary containing counts of documents, pages, passages, and pages with warnings.
    FAILS WITH: ComponentError on invalid manifest, missing files, or serialization failure."""
    # STEP 1: Read and parse manifest.json | takes: manifest_path | gives: manifest_items
    manifest_p = Path(manifest_path).resolve()
    if not manifest_p.exists():
        trace("C4", "Manifest file not found", path=str(manifest_path))
        raise ComponentError("C4", "read_manifest", f"Manifest file does not exist: {manifest_path}")

    try:
        with open(manifest_p, "r", encoding="utf-8") as f:
            raw_manifest = json.load(f)
    except Exception as exc:
        trace("C4", "Manifest JSON parse failed", error=str(exc))
        raise ComponentError("C4", "read_manifest", f"Failed to parse manifest JSON: {exc}") from exc

    if isinstance(raw_manifest, list):
        for raw_item in raw_manifest:
            if isinstance(raw_item, dict):
                raw_role = raw_item.get("evidence_role")
                raw_doc_id = raw_item.get("document_id", "unknown")
                if raw_role is not None and raw_role not in ALLOWED_EVIDENCE_ROLES:
                    trace("C4", "Invalid evidence role in manifest", doc_id=raw_doc_id, evidence_role=str(raw_role))
                    raise ComponentError("C4", "evidence_role", f"{raw_doc_id} has invalid evidence_role '{raw_role}'")

    manifest_items = validate_manifest(raw_manifest)
    trace("C4", "Manifest loaded successfully", document_count=len(manifest_items))

    # STEP 2: Process documents and extract passages per page | takes: manifest_items | gives: all_passages, doc_reports
    all_passages: list[Passage] = []
    doc_reports: list[dict[str, Any]] = []
    total_pages = 0
    total_warning_pages = 0

    for idx, doc in enumerate(manifest_items):
        raw_doc = raw_manifest[idx] if isinstance(raw_manifest, list) and idx < len(raw_manifest) and isinstance(raw_manifest[idx], dict) else {}
        if doc.evidence_role is not None:
            if doc.evidence_role not in ALLOWED_EVIDENCE_ROLES:
                raise ComponentError("C4", "evidence_role", f"{doc.document_id} has invalid evidence_role '{doc.evidence_role}'")
            evidence_role = doc.evidence_role
        else:
            evidence_role = "observation"
        default_tags = list(raw_doc.get("component_tags") or raw_doc.get("default_component_tags") or [])

        resolved_pdf = _resolve_doc_path(doc.file_path, manifest_p.parent)
        pages_data = extract_pages(resolved_pdf)
        total_pages += len(pages_data)

        doc_warning_pages: list[int] = []
        doc_passages_count = 0

        for page in pages_data:
            page_num = page["page_number"]
            page_text = page["text"]
            page_warnings = page["warnings"]

            if page_warnings:
                doc_warning_pages.append(page_num)
                total_warning_pages += 1

            chunks = _split_page_into_passages(page_text, max_chars=max_chars, overlap_chars=overlap_chars)
            for chunk_idx, chunk_text in enumerate(chunks):
                passage_id = f"{doc.document_id}-p{page_num}-{chunk_idx}"
                assigned_tags = list(default_tags)
                if tag_components_enabled:
                    kw_tags = tag_components(chunk_text)
                    for t in kw_tags:
                        if t not in assigned_tags:
                            assigned_tags.append(t)

                passage_obj = Passage(
                    passage_id=passage_id,
                    document_id=doc.document_id,
                    document_title=doc.title,
                    source_url=doc.source_url,
                    publisher=doc.publisher,
                    document_type=doc.document_type,
                    publication_date=doc.publication_date,
                    page_number=page_num,
                    timestamp=None,
                    text=chunk_text,
                    case_id=doc.case_id,
                    component_tags=assigned_tags,
                    evidence_role=evidence_role,
                    ingestion_method="local_pypdf",
                    extraction_warnings=list(page_warnings),
                    split=doc.split,
                )
                all_passages.append(passage_obj)
                doc_passages_count += 1

        doc_reports.append({
            "document_id": doc.document_id,
            "pages": len(pages_data),
            "passages": doc_passages_count,
            "warning_pages": doc_warning_pages,
        })
        trace("C4", "Document processed", doc_id=doc.document_id, pages=len(pages_data), passages=doc_passages_count)

    # STEP 3: Write passages.jsonl file | takes: all_passages, out_path | gives: written jsonl file
    out_p = Path(out_path).resolve()
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            for passage in all_passages:
                f.write(passage.model_dump_json() + "\n")
        trace("C4", "Serialized passages.jsonl", out_path=str(out_p), count=len(all_passages))
    except Exception as exc:
        trace("C4", "Failed writing passages.jsonl", error=str(exc))
        raise ComponentError("C4", "write_passages", f"Failed writing passages to {out_p}: {exc}") from exc

    # STEP 4: Build summary dictionary and optionally display report | takes: all_passages, doc_reports | gives: summary
    summary: dict[str, Any] = {
        "documents": len(manifest_items),
        "pages": total_pages,
        "passages": len(all_passages),
        "pages_with_warnings": total_warning_pages,
        "pages with warnings": total_warning_pages,
    }

    if report:
        print_report(doc_reports, summary)
    else:
        print(
            f"Passage ingestion complete: {summary['documents']} documents, "
            f"{summary['pages']} pages, {summary['passages']} passages, "
            f"{summary['pages_with_warnings']} warning pages."
        )

    return summary


def merge_evolus_summaries(passages_path: str | Path, evolus_raw_dir: str | Path) -> None:
    """IN: passages_path (str or Path) to passages.jsonl, evolus_raw_dir (str or Path) to Evolus output directory.
    OUT: None.
    FAILS WITH: NotImplementedError until real Evolus output format is inspected."""
    # STEP 1: Raise NotImplementedError to avoid guessing unknown Evolus data formats | takes: passages_path, evolus_raw_dir | gives: NotImplementedError
    trace("C4", "merge_evolus_summaries stub called", passages_path=str(passages_path))
    raise NotImplementedError(
        "merge_evolus_summaries is a documented hook pending real Evolus output format inspection"
    )


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS/FAIL and exits).
    FAILS WITH: SystemExit on test failure."""
    trace("C4", "Starting self-test for build_passages")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # STEP 1: Create synthetic test PDFs with aviation keywords and warning conditions | takes: temp_path | gives: pdf_1, pdf_2
            pdf1_text_p1 = (
                "The main landing gear actuator cylinder suffered fatigue cracking. "
                "Hydraulic pressure dropped immediately following landing gear extension."
            )
            pdf1_text_p2 = (
                "Post-incident teardown of the turbine engine revealed abnormal wear on fan blade root attachments. "
                "The fuel system shutoff valve operated as commanded."
            )
            pdf_1 = temp_path / "test_doc_1.pdf"
            pdf_1.write_bytes(_create_test_pdf_bytes([pdf1_text_p1, pdf1_text_p2]))

            # PDF 2 has 1 page with short text triggering empty_text warning
            pdf_2 = temp_path / "test_doc_2.pdf"
            pdf_2.write_bytes(_create_test_pdf_bytes(["short"]))

            # STEP 2: Write temporary test manifest referencing created PDFs | takes: pdf_1, pdf_2 | gives: manifest_path
            manifest_data = [
                {
                    "document_id": "TEST-DOC-001",
                    "title": "Factual Report Test",
                    "file_path": str(pdf_1),
                    "source_url": "https://data.ntsb.gov/test1.pdf",
                    "publisher": "National Transportation Safety Board",
                    "document_type": "factual_report",
                    "publication_date": "2026-01-01",
                    "retrieval_date": "2026-01-02",
                    "case_id": "TEST-CASE-01",
                    "split": "prediction",
                    "notes": "Test document 1",
                },
                {
                    "document_id": "TEST-DOC-002",
                    "title": "Short Document Test",
                    "file_path": str(pdf_2),
                    "source_url": "https://data.ntsb.gov/test2.pdf",
                    "publisher": "Federal Aviation Administration",
                    "document_type": "advisory_circular",
                    "publication_date": "2026-01-01",
                    "retrieval_date": "2026-01-02",
                    "case_id": "TEST-CASE-01",
                    "split": "prediction",
                    "notes": "Test document 2 with warning",
                    "evidence_role": "guidance",
                },
            ]
            manifest_path = temp_path / "manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f)

            # STEP 3: Execute build_passages and inspect output summary | takes: manifest_path, out_path | gives: summary
            out_path = temp_path / "passages.jsonl"
            summary = build_passages(
                manifest_path=manifest_path,
                out_path=out_path,
                max_chars=200,
                overlap_chars=30,
                report=True,
            )

            if summary["documents"] != 2:
                print(f"FAIL: Expected 2 documents, got {summary['documents']}")
                sys.exit(1)
            if summary["pages"] != 3:
                print(f"FAIL: Expected 3 pages, got {summary['pages']}")
                sys.exit(1)
            if summary["pages_with_warnings"] < 1:
                print("FAIL: Expected at least 1 page with warnings")
                sys.exit(1)

            # STEP 4: Validate generated passages against contract rules | takes: out_path | gives: verified passages
            passages: list[Passage] = []
            seen_ids: set[str] = set()
            with open(out_path, "r", encoding="utf-8") as f:
                for line_idx, line in enumerate(f, start=1):
                    p_raw = json.loads(line.strip())
                    p = validate_passage(p_raw)
                    passages.append(p)

                    # Assert every passage has a page_number
                    if p.page_number is None or p.page_number < 1:
                        print(f"FAIL: Passage {p.passage_id} missing valid page_number")
                        sys.exit(1)

                    # Assert passage_ids are unique
                    if p.passage_id in seen_ids:
                        print(f"FAIL: Duplicate passage_id detected: {p.passage_id}")
                        sys.exit(1)
                    seen_ids.add(p.passage_id)

                    # Assert no passage text is empty
                    if not p.text.strip():
                        print(f"FAIL: Passage {p.passage_id} has empty text")
                        sys.exit(1)

                    # Assert ingestion method
                    if p.ingestion_method != "local_pypdf":
                        print(f"FAIL: Ingestion method expected 'local_pypdf', got '{p.ingestion_method}'")
                        sys.exit(1)

                    # Assert evidence_role: document with 'guidance' gets 'guidance', document without it gets 'observation'
                    if p.document_id == "TEST-DOC-001" and p.evidence_role != "observation":
                        print(f"FAIL: Passage {p.passage_id} expected evidence_role 'observation', got '{p.evidence_role}'")
                        sys.exit(1)
                    if p.document_id == "TEST-DOC-002" and p.evidence_role != "guidance":
                        print(f"FAIL: Passage {p.passage_id} expected evidence_role 'guidance', got '{p.evidence_role}'")
                        sys.exit(1)

            # Assert component tags matched expected keywords
            p1_tags = [p.component_tags for p in passages if p.document_id == "TEST-DOC-001" and p.page_number == 1]
            flattened_p1_tags = {tag for tags in p1_tags for tag in tags}
            if "landing_gear" not in flattened_p1_tags or "hydraulic_system" not in flattened_p1_tags:
                print(f"FAIL: Expected landing_gear and hydraulic_system tags, got {flattened_p1_tags}")
                sys.exit(1)

            p2_tags = [p.component_tags for p in passages if p.document_id == "TEST-DOC-001" and p.page_number == 2]
            flattened_p2_tags = {tag for tags in p2_tags for tag in tags}
            if "engine" not in flattened_p2_tags or "fuel_system" not in flattened_p2_tags:
                print(f"FAIL: Expected engine and fuel_system tags, got {flattened_p2_tags}")
                sys.exit(1)

            # STEP 5: Verify invalid evidence_role raises ComponentError | takes: invalid_manifest | gives: verified ComponentError
            invalid_manifest_path = temp_path / "invalid_manifest.json"
            with open(invalid_manifest_path, "w", encoding="utf-8") as f:
                json.dump([
                    {
                        "document_id": "TEST-DOC-INVALID",
                        "title": "Invalid Role Document",
                        "file_path": str(pdf_1),
                        "source_url": "https://data.ntsb.gov/invalid.pdf",
                        "publisher": "Federal Aviation Administration",
                        "document_type": "advisory_circular",
                        "publication_date": "2026-01-01",
                        "retrieval_date": "2026-01-02",
                        "case_id": "TEST-CASE-01",
                        "split": "prediction",
                        "notes": "Invalid evidence role test",
                        "evidence_role": "bogus_role",
                    }
                ], f)

            try:
                build_passages(manifest_path=invalid_manifest_path, out_path=temp_path / "bad.jsonl")
                print("FAIL: Expected ComponentError on invalid evidence_role")
                sys.exit(1)
            except ComponentError as err:
                if err.component != "C4" or err.step != "evidence_role":
                    print(f"FAIL: Expected ComponentError('C4', 'evidence_role'), got ('{err.component}', '{err.step}')")
                    sys.exit(1)
                expected_msg = "TEST-DOC-INVALID has invalid evidence_role 'bogus_role'"
                if expected_msg not in err.message:
                    print(f"FAIL: Expected message '{expected_msg}', got '{err.message}'")
                    sys.exit(1)

            # STEP 6: Validate merge_evolus_summaries raises NotImplementedError | takes: dummy paths | gives: verified exception
            try:
                merge_evolus_summaries("dummy_path", "dummy_dir")
                print("FAIL: Expected merge_evolus_summaries to raise NotImplementedError")
                sys.exit(1)
            except NotImplementedError:
                pass

        # STEP 7: Report PASS on self-test success | takes: None | gives: stdout PASS
        trace("C4", "Self-test passed for build_passages")
        print("PASS")
        sys.exit(0)
    except Exception as exc:
        trace("C4", "Self-test failed for build_passages", error=str(exc))
        print(f"FAIL: {exc}")
        sys.exit(1)


def main() -> None:
    """IN: Command line arguments from sys.argv.
    OUT: None.
    FAILS WITH: SystemExit on CLI execution error."""
    # STEP 1: Parse CLI arguments | takes: sys.argv | gives: parsed args
    parser = argparse.ArgumentParser(description="Build passage chunks from document manifest")
    parser.add_argument("--manifest", default=None, help="Path to input manifest.json")
    parser.add_argument("--out", default=None, help="Path to output passages.jsonl")
    parser.add_argument("--max-chars", type=int, default=1500, help="Maximum passage character length")
    parser.add_argument("--overlap-chars", type=int, default=150, help="Passage overlap characters")
    parser.add_argument("--report", action="store_true", help="Print per-document table of page counts and warning pages")
    parser.add_argument("--selftest", action="store_true", help="Execute component self-test")
    args = parser.parse_args()

    # STEP 2: Dispatch execution based on flags | takes: args | gives: execution result
    if args.selftest:
        run_selftest()
    else:
        manifest_path = args.manifest
        out_path = args.out
        if manifest_path is None:
            try:
                from common.config import get_data_dir
                manifest_path = get_data_dir() / "manifest.json"
            except Exception:
                manifest_path = Path("data/manifest.json")
        if out_path is None:
            try:
                from common.config import get_data_dir
                out_path = get_data_dir() / "processed" / "passages.jsonl"
            except Exception:
                out_path = Path("data/processed/passages.jsonl")

        build_passages(
            manifest_path=manifest_path,
            out_path=out_path,
            max_chars=args.max_chars,
            overlap_chars=args.overlap_chars,
            report=args.report,
        )


if __name__ == "__main__":
    main()

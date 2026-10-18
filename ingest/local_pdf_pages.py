# ============================================================
# COMPONENT: C4 - Local PDF Page Extractor
# PURPOSE: Extract text and metadata per page from PDF files using pypdf with diagnostic quality flags.
# INPUT: Local file path to PDF document (str or Path).
# OUTPUT: List of dictionaries containing page_number, text, char_count, and warnings.
# CALLED BY: ingest/build_passages.py, pipeline orchestrators      CALLS: pypdf, common/trace.py
# IF IT BREAKS, CHECK: PDF file existence and permissions, corrupted PDF structure, pypdf text extraction support.
# ============================================================

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from common.trace import ComponentError, trace


def _create_test_pdf_bytes(pages_text: list[str]) -> bytes:
    """IN: pages_text (list of str for each page's text).
    OUT: bytes containing valid PDF-1.4 binary data.
    FAILS WITH: ComponentError on byte assembly error."""
    # STEP 1: Construct PDF object definitions for font, catalog, pages, and streams | takes: pages_text | gives: all_objs
    try:
        font_obj = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        page_obj_nums: list[int] = []
        content_obj_nums: list[int] = []
        for idx in range(len(pages_text)):
            page_num = 4 + idx * 2
            content_num = page_num + 1
            page_obj_nums.append(page_num)
            content_obj_nums.append(content_num)

        kids_str = " ".join(f"{n} 0 R" for n in page_obj_nums)
        pages_obj = f"<< /Type /Pages /Kids [{kids_str}] /Count {len(pages_text)} >>".encode("ascii")
        catalog_obj = b"<< /Type /Catalog /Pages 3 0 R >>"

        all_objs: list[bytes] = [font_obj, catalog_obj, pages_obj]
        for idx, text in enumerate(pages_text):
            content_num = content_obj_nums[idx]
            page_dict = (
                f"<< /Type /Page /Parent 3 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content_num} 0 R /Resources << /Font << /F1 1 0 R >> >> >>"
            ).encode("ascii")
            escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream_bytes = f"BT /F1 12 Tf 72 712 Td ({escaped_text}) Tj ET\n".encode("latin-1")
            content_dict = f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"endstream"
            all_objs.append(page_dict)
            all_objs.append(content_dict)

        # STEP 2: Compute cross-reference table byte offsets and serialize PDF structure | takes: all_objs | gives: complete PDF bytes
        out = bytearray(b"%PDF-1.4\n")
        obj_offsets: list[int] = [0]
        for obj_idx, obj_data in enumerate(all_objs, start=1):
            obj_offsets.append(len(out))
            out.extend(f"{obj_idx} 0 obj\n".encode("ascii") + obj_data + b"\nendobj\n")

        xref_offset = len(out)
        out.extend(f"xref\n0 {len(obj_offsets)}\n".encode("ascii"))
        out.extend(b"0000000000 65535 f \n")
        for off in obj_offsets[1:]:
            out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
        out.extend(f"trailer\n<< /Size {len(obj_offsets)} /Root 2 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
        return bytes(out)
    except Exception as exc:
        raise ComponentError("C4", "create_test_pdf", f"Failed generating test PDF bytes: {exc}") from exc


def extract_pages(pdf_path: str | Path) -> list[dict[str, Any]]:
    """IN: pdf_path (str or Path) pointing to target PDF file on disk.
    OUT: list[dict] where each dict contains page_number (1-based int), text (str), char_count (int), warnings (list[str]).
    FAILS WITH: ComponentError if file is missing, unreadable, or corrupted."""
    # STEP 1: Validate file path existence and type | takes: pdf_path | gives: resolved_path
    resolved_path = Path(pdf_path).resolve()
    if not resolved_path.exists():
        trace("C4", "Target PDF file not found", path=str(pdf_path))
        raise ComponentError("C4", "validate_pdf_path", f"PDF file not found at: {pdf_path}")
    if not resolved_path.is_file():
        trace("C4", "Target PDF path is not a file", path=str(pdf_path))
        raise ComponentError("C4", "validate_pdf_path", f"PDF path is not a regular file: {pdf_path}")

    # STEP 2: Open and parse PDF structure using pypdf | takes: resolved_path | gives: reader
    try:
        reader = PdfReader(str(resolved_path))
        total_pages = len(reader.pages)
        trace("C4", "Opened PDF document for extraction", path=resolved_path.name, total_pages=total_pages)
    except Exception as exc:
        trace("C4", "Failed to open PDF document", path=str(pdf_path), error=str(exc))
        raise ComponentError("C4", "open_pdf", f"Unable to parse PDF file at {pdf_path}: {exc}") from exc

    # STEP 3: Iterate through document pages and extract text | takes: reader.pages | gives: page_records
    page_records: list[dict[str, Any]] = []
    try:
        for idx, page in enumerate(reader.pages, start=1):
            raw_text = page.extract_text() or ""
            cleaned_text = raw_text.strip()
            char_count = len(cleaned_text)
            warnings: list[str] = []
            if char_count < 30:
                warnings.append("empty_text")
            page_records.append({
                "page_number": idx,
                "text": cleaned_text,
                "char_count": char_count,
                "warnings": warnings,
            })
    except Exception as exc:
        trace("C4", "Page text extraction failed", path=str(pdf_path), error=str(exc))
        raise ComponentError("C4", "extract_page_text", f"Error extracting page text from {pdf_path}: {exc}") from exc

    # STEP 4: Detect mostly empty documents and assign possible_scan flags | takes: page_records | gives: flagged_records
    empty_count = sum(1 for p in page_records if "empty_text" in p["warnings"])
    is_mostly_empty = total_pages > 0 and (empty_count > total_pages / 2)
    if is_mostly_empty:
        for p in page_records:
            if "empty_text" in p["warnings"] and "possible_scan" not in p["warnings"]:
                p["warnings"].append("possible_scan")

    # STEP 5: Log extraction summary and return final page list | takes: flagged_records | gives: page_records
    trace(
        "C4",
        "Completed PDF page extraction",
        path=resolved_path.name,
        total_pages=total_pages,
        empty_pages=empty_count,
        possible_scan=is_mostly_empty,
    )
    return page_records


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS/FAIL and exits).
    FAILS WITH: SystemExit on test failure."""
    trace("C4", "Starting self-test for local_pdf_pages")
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # STEP 1: Test valid text page extraction without warnings | takes: synthetic valid PDF | gives: verified records
            valid_text_1 = "This is a full page description of the aircraft hydraulic actuator assembly with sufficient characters."
            valid_text_2 = "Second page contains extensive mechanical inspection notes regarding landing gear attachment pins."
            pdf_bytes = _create_test_pdf_bytes([valid_text_1, valid_text_2])
            normal_pdf = temp_path / "normal_test.pdf"
            normal_pdf.write_bytes(pdf_bytes)

            records = extract_pages(normal_pdf)
            if len(records) != 2:
                print(f"FAIL: Expected 2 pages, got {len(records)}")
                sys.exit(1)
            if records[0]["page_number"] != 1 or records[1]["page_number"] != 2:
                print("FAIL: Page numbers are not 1-based sequential integers")
                sys.exit(1)
            if records[0]["warnings"] or records[1]["warnings"]:
                print("FAIL: Unexpected extraction warnings on full-text pages")
                sys.exit(1)
            if records[0]["char_count"] < 30 or records[1]["char_count"] < 30:
                print("FAIL: Character count mismatch on full-text pages")
                sys.exit(1)

            # STEP 2: Test empty text warning on single isolated empty page | takes: 2-page PDF (1 full, 1 short) | gives: verified empty_text warning
            mixed_pdf = temp_path / "mixed_test.pdf"
            mixed_pdf.write_bytes(_create_test_pdf_bytes([valid_text_1, "short note"]))
            mixed_records = extract_pages(mixed_pdf)
            if len(mixed_records) != 2:
                print("FAIL: Expected 2 pages in mixed document")
                sys.exit(1)
            if mixed_records[0]["warnings"]:
                print("FAIL: Page 1 in mixed document should not have warnings")
                sys.exit(1)
            if mixed_records[1]["warnings"] != ["empty_text"]:
                print(f"FAIL: Expected ['empty_text'] for page 2, got {mixed_records[1]['warnings']}")
                sys.exit(1)

            # STEP 3: Test possible_scan warning when most pages are empty | takes: all-empty PDF | gives: verified possible_scan warning
            scan_pdf = temp_path / "scan_test.pdf"
            scan_pdf.write_bytes(_create_test_pdf_bytes(["p1", "p2"]))
            scan_records = extract_pages(scan_pdf)
            if len(scan_records) != 2:
                print("FAIL: Expected 2 pages in scan document")
                sys.exit(1)
            for rec in scan_records:
                if "empty_text" not in rec["warnings"] or "possible_scan" not in rec["warnings"]:
                    print(f"FAIL: Expected both empty_text and possible_scan in scan document, got {rec['warnings']}")
                    sys.exit(1)

            # STEP 4: Test missing file error behavior | takes: non-existent file path | gives: verified ComponentError
            non_existent_file = temp_path / "does_not_exist.pdf"
            try:
                extract_pages(non_existent_file)
                print("FAIL: Expected ComponentError for non-existent PDF file")
                sys.exit(1)
            except ComponentError as err:
                if "validate_pdf_path" not in err.step:
                    print(f"FAIL: Unexpected error step on missing file: {err.step}")
                    sys.exit(1)

        # STEP 5: Output PASS and exit cleanly | takes: None | gives: stdout PASS
        trace("C4", "Self-test passed for local_pdf_pages")
        print("PASS")
        sys.exit(0)
    except Exception as exc:
        trace("C4", "Self-test failed for local_pdf_pages", error=str(exc))
        print(f"FAIL: {exc}")
        sys.exit(1)


def main() -> None:
    """IN: CLI arguments from sys.argv.
    OUT: None.
    FAILS WITH: SystemExit on error."""
    # STEP 1: Parse command line arguments | takes: sys.argv | gives: parsed args
    parser = argparse.ArgumentParser(description="Extract pages and metadata from local PDF")
    parser.add_argument("pdf_path", nargs="?", help="Path to PDF file to extract")
    parser.add_argument("--selftest", action="store_true", help="Execute component self-test")
    args = parser.parse_args()

    # STEP 2: Dispatch to self-test or extract pages for target file | takes: args | gives: console output
    if args.selftest:
        run_selftest()
    elif args.pdf_path:
        pages = extract_pages(args.pdf_path)
        print(f"Extracted {len(pages)} pages from {args.pdf_path}")
        for p in pages:
            print(f"Page {p['page_number']}: chars={p['char_count']}, warnings={p['warnings']}")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

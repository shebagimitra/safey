# ============================================================
# COMPONENT: C3 - Evolus Client
# PURPOSE: Issue AI tool processing requests to Evolus REST API for prediction-split documents.
# INPUT: document_id, tool name, tool options dictionary, optional client and data_dir overrides.
# OUTPUT: Execution result dictionary, cached JSON in data/processed/evolus_raw, usage logged in _usage.jsonl.
# CALLED BY: Pipeline orchestrators, CLI      CALLS: common/config.py, common/models.py, common/trace.py, httpx
# IF IT BREAKS, CHECK: EVOLUS_BASE_URL format, EVOLUS_API_KEY, document split leakage, network connectivity.
# ============================================================

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from common.config import get_data_dir, get_settings
from common.models import DocumentManifest, DocumentManifestList
from common.trace import ComponentError, trace

# Constant maximum file upload size in megabytes
# from the run-media description in the OpenAPI document
MAX_FILE_MB: int = 20


def _validate_base_url(url_str: str) -> str:
    """IN: url_str (str) representing base URL from configuration or client.
    OUT: Clean host-only URL string without path, query, or fragment.
    FAILS WITH: ComponentError if base URL contains path, query, or fragment components."""
    # STEP 1: Parse and validate that URL has scheme, host, and no path components | takes: url_str | gives: validated host URL
    parsed = urllib.parse.urlsplit(url_str.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ComponentError("C3", "config", "EVOLUS_BASE_URL must be the host only, e.g. https://api.evolus.ai")
    path = parsed.path.rstrip("/")
    if path != "" or parsed.query or parsed.fragment:
        raise ComponentError("C3", "config", "EVOLUS_BASE_URL must be the host only, e.g. https://api.evolus.ai")
    return f"{parsed.scheme}://{parsed.netloc}"


def _resolve_manifest(document_id: str, data_dir: Path) -> DocumentManifest:
    """IN: document_id (str), data_dir (Path) local data directory.
    OUT: DocumentManifest instance for the resolved document.
    FAILS WITH: ComponentError if manifest cannot be loaded or document is not found."""
    # STEP 1: Find candidate manifest file paths across data_dir and project root | takes: data_dir | gives: manifest_path
    candidate_paths = [
        data_dir / "manifest.json",
        Path(__file__).resolve().parent.parent / "data" / "manifest.json",
        Path(__file__).resolve().parent.parent / "contracts" / "fixtures" / "manifest.json",
    ]
    manifest_path: Optional[Path] = None
    for cp in candidate_paths:
        if cp.exists():
            manifest_path = cp
            break
    if manifest_path is None:
        raise ComponentError("C3", "manifest", f"Manifest file not found in candidates: {[str(p) for p in candidate_paths]}")

    # STEP 2: Load and parse manifest JSON, matching document_id | takes: manifest_path, document_id | gives: DocumentManifest
    try:
        content = manifest_path.read_text(encoding="utf-8")
        manifest_list = DocumentManifestList.model_validate_json(content).root
    except Exception as exc:
        raise ComponentError("C3", "manifest", f"Failed parsing manifest at {manifest_path}: {exc}") from exc

    for doc in manifest_list:
        if doc.document_id == document_id:
            return doc

    raise ComponentError("C3", "manifest", f"Document '{document_id}' not found in manifest")


def _resolve_pdf_path(raw_file_path: str, data_dir: Path) -> Path:
    """IN: raw_file_path (str) from manifest, data_dir (Path) local data directory.
    OUT: Path to verified existing PDF file.
    FAILS WITH: ComponentError if PDF file cannot be resolved on disk."""
    # STEP 1: Search candidate locations for referenced PDF file | takes: raw_file_path, data_dir | gives: resolved Path
    clean_path = raw_file_path.replace("FIXTURE:", "")
    p = Path(clean_path)
    if p.is_absolute() and p.exists():
        return p

    project_root = Path(__file__).resolve().parent.parent
    candidates = [
        data_dir / clean_path,
        project_root / clean_path,
        project_root / "data" / clean_path,
        data_dir / p.name,
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()

    raise ComponentError("C3", "resolve_file", f"Document file not found: {raw_file_path}")


def run_tool(
    document_id: str,
    tool: str,
    options: dict[str, Any] | str,
    *,
    force: bool = False,
    client: Optional[httpx.Client] = None,
    data_dir: Optional[Path | str] = None,
) -> dict[str, Any]:
    """IN: document_id (str), tool (str), options (dict or JSON str), force (bool), client (optional httpx.Client), data_dir (optional Path or str).
    OUT: Result dictionary with document_id, tool, raw_path, ok, cached, credits_consumed, warnings.
    FAILS WITH: ComponentError on leakage violation, oversize file, network/HTTP failure, or invalid configuration."""
    # STEP 1: Resolve manifest entry, guard prediction split, and verify file size | takes: document_id, data_dir | gives: doc, pdf_path, file_bytes
    effective_data_dir = Path(data_dir).resolve() if data_dir is not None else get_data_dir()
    doc = _resolve_manifest(document_id, effective_data_dir)

    # Evolus outputs feed the UI before reveal, so truth documents must never be sent.
    if doc.split != "prediction":
        raise ComponentError("C3", "leakage", "refusing to send a non-prediction document to Evolus")

    pdf_path = _resolve_pdf_path(doc.file_path, effective_data_dir)
    file_bytes = pdf_path.stat().st_size
    if file_bytes > MAX_FILE_MB * 1024 * 1024:
        raise ComponentError("C3", "file_size", f"File size {file_bytes} bytes exceeds maximum allowed limit {MAX_FILE_MB} MB")

    raw_dir = effective_data_dir / "processed" / "evolus_raw"
    raw_path = raw_dir / f"{document_id}.{tool}.json"

    # STEP 2: Check for existing cached raw output when force is False | takes: raw_path, force | gives: cached result dict
    if raw_path.exists() and not force:
        cached_data = load_saved(document_id, tool, data_dir=effective_data_dir)
        if cached_data is not None:
            resp_part = cached_data.get("response", {})
            meta_part = cached_data.get("meta", {})
            credits_consumed = meta_part.get("credits_consumed", resp_part.get("creditsConsumed"))
            truncated = bool(resp_part.get("truncated", False))
            warnings = ["truncated"] if truncated else []
            trace("C3", "Cache hit", document_id=document_id, tool=tool, status=200, elapsed=0.0, credits_consumed=0)
            return {
                "document_id": document_id,
                "tool": tool,
                "raw_path": str(raw_path),
                "ok": True,
                "cached": True,
                "credits_consumed": credits_consumed,
                "warnings": warnings,
            }

    # STEP 3: Dispatch POST request to Evolus with retries | takes: active_client, tool, options, pdf_path | gives: response, elapsed_s
    client_provided = client is not None
    active_client: Optional[httpx.Client] = None
    if client_provided:
        active_client = client
        if active_client.base_url and str(active_client.base_url).strip() not in ("", "/"):
            _validate_base_url(str(active_client.base_url))
            url = f"/api/v1/ai-tools/{tool}/run-media"
        else:
            try:
                base_host = _validate_base_url(get_settings().EVOLUS_BASE_URL)
            except Exception:
                base_host = "https://api.evolus.ai"
            url = f"{base_host}/api/v1/ai-tools/{tool}/run-media"
    else:
        settings = get_settings()
        base_host = _validate_base_url(settings.EVOLUS_BASE_URL)
        auth_headers = {
            "X-ApiKey": settings.EVOLUS_API_KEY,
            "X-Application-Id": settings.EVOLUS_APP_ID,
        }
        active_client = httpx.Client(base_url=base_host, headers=auth_headers)
        url = f"/api/v1/ai-tools/{tool}/run-media"

    if isinstance(options, str):
        try:
            options_dict = json.loads(options)
        except Exception:
            options_dict = {"raw": options}
        options_query = options
    else:
        options_dict = options
        options_query = json.dumps(options)

    file_bytes_content = pdf_path.read_bytes()
    files = {"file": (pdf_path.name, file_bytes_content, "application/pdf")}
    params = {"options": options_query}

    t0 = time.perf_counter()
    response: Optional[httpx.Response] = None

    try:
        for attempt in range(2):
            try:
                response = active_client.post(
                    url,
                    params=params,
                    files=files,
                    timeout=180.0,
                )
            except httpx.TransportError as exc:
                if attempt == 0:
                    trace("C3", "Transport error on initial attempt, retrying once", error=type(exc).__name__)
                    continue
                raise ComponentError("C3", "http_post", f"HTTP transport error after retry: {exc}") from exc
            except Exception as exc:
                raise ComponentError("C3", "http_post", f"Unexpected request failure: {exc}") from exc

            if response.status_code == 429 and attempt == 0:
                retry_after_hdr = response.headers.get("Retry-After", "1")
                try:
                    retry_after_s = float(retry_after_hdr)
                except (ValueError, TypeError):
                    retry_after_s = 1.0
                sleep_s = max(0.0, min(retry_after_s, 65.0))
                trace("C3", "Rate limited (429), sleeping and retrying once", retry_after=sleep_s)
                time.sleep(sleep_s)
                continue

            break
    finally:
        if not client_provided and active_client is not None:
            active_client.close()

    elapsed_s = round(time.perf_counter() - t0, 3)

    if response is None:
        raise ComponentError("C3", "http_post", "No response received from Evolus endpoint")

    # STEP 4: Handle non-200 HTTP responses | takes: response, elapsed_s | gives: ComponentError raised
    if response.status_code != 200:
        error_code = "unknown"
        trace_id = "unknown"
        detail = response.text
        try:
            err_json = response.json()
            if isinstance(err_json, dict):
                error_code = err_json.get("errorCode", "unknown")
                trace_id = err_json.get("traceId", "unknown")
                detail = err_json.get("detail", detail)
        except Exception:
            pass
        trace("C3", "Tool execution failure", document_id=document_id, tool=tool, status=response.status_code, elapsed=elapsed_s, credits_consumed=None)
        raise ComponentError("C3", "http_post", f"HTTP {response.status_code} errorCode={error_code} traceId={trace_id}: {detail}")

    # STEP 5: Persist raw response and append telemetry to usage log | takes: response, elapsed_s | gives: saved files
    response_json = response.json()
    credits_consumed = response_json.get("creditsConsumed")
    is_truncated = bool(response_json.get("truncated", False))
    warnings = ["truncated"] if is_truncated else []

    meta = {
        "document_id": document_id,
        "tool": tool,
        "options": options_dict,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed_s,
        "http_status": response.status_code,
        "file_name": pdf_path.name,
        "file_bytes": file_bytes,
        "source_split": doc.split,
        "credits_consumed": credits_consumed,
        "label": "GENERATED_BY_EVOLUS_NOT_VERIFIED",
    }
    payload = {
        "meta": meta,
        "response": response_json,
    }

    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    usage_file = raw_dir / "_usage.jsonl"
    usage_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "document_id": document_id,
        "tool": tool,
        "status": response.status_code,
        "elapsed_s": elapsed_s,
        "credits_consumed": credits_consumed,
    }
    with open(usage_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(usage_entry) + "\n")

    trace("C3", "Tool execution success", document_id=document_id, tool=tool, status=response.status_code, elapsed=elapsed_s, credits_consumed=credits_consumed)

    # STEP 6: Assemble and return tool execution result summary | takes: response_json, raw_path, elapsed_s | gives: return dict
    return {
        "document_id": document_id,
        "tool": tool,
        "raw_path": str(raw_path),
        "ok": True,
        "cached": False,
        "credits_consumed": credits_consumed,
        "warnings": warnings,
    }


def extract_document(document_id: str, **kw: Any) -> dict[str, Any]:
    """IN: document_id (str), optional keyword arguments forwarded to run_tool.
    OUT: Execution result dictionary from doc-structure tool.
    FAILS WITH: ComponentError on leakage, resolution, configuration, or API error."""
    # STEP 1: Forward call to run_tool with doc-structure tool and data options | takes: document_id, kw | gives: result dict
    return run_tool(document_id, "doc-structure", {"format": "json", "content": "data"}, **kw)


def summarize_document(
    document_id: str,
    style: str = "bullets",
    length: str = "medium",
    **kw: Any,
) -> dict[str, Any]:
    """IN: document_id (str), style (str), length (str), optional keyword arguments forwarded to run_tool.
    OUT: Execution result dictionary from summarize tool.
    FAILS WITH: ComponentError on leakage, resolution, configuration, or API error."""
    # STEP 1: Forward call to run_tool with summarize tool and options | takes: document_id, style, length, kw | gives: result dict
    return run_tool(document_id, "summarize", {"style": style, "length": length}, **kw)


def load_saved(document_id: str, tool: str, *, data_dir: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    """IN: document_id (str), tool (str), optional data_dir (Path or str).
    OUT: Parsed raw output dictionary or None if file does not exist.
    FAILS WITH: ComponentError if reading or parsing existing file fails."""
    # STEP 1: Determine path to saved raw JSON file | takes: document_id, tool, data_dir | gives: raw_path
    if data_dir is not None:
        effective_data_dir = Path(data_dir).resolve()
    else:
        effective_data_dir = get_data_dir()
    raw_path = effective_data_dir / "processed" / "evolus_raw" / f"{document_id}.{tool}.json"

    # STEP 2: Read and parse JSON content if file exists | takes: raw_path | gives: dict or None
    if not raw_path.exists():
        return None
    try:
        return json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ComponentError("C3", "load_saved", f"Failed reading saved raw output at {raw_path}: {exc}") from exc


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (executes test suite and prints PASS/FAIL per case).
    FAILS WITH: SystemExit on test assertion failure."""
    # STEP 1: Setup isolated fixture environment in temporary directory | takes: contracts/fixtures | gives: temp_data_dir, test_pdf
    fixtures_dir = Path(__file__).resolve().parent.parent / "contracts" / "fixtures"
    fixture_manifest_file = fixtures_dir / "manifest.json"
    if not fixture_manifest_file.exists():
        print(f"FAIL: Fixture manifest not found at {fixture_manifest_file}")
        sys.exit(1)

    temp_dir = tempfile.TemporaryDirectory()
    temp_path = Path(temp_dir.name).resolve()

    try:
        # Write copy of fixture manifest into temporary data directory
        (temp_path / "manifest.json").write_text(fixture_manifest_file.read_text(encoding="utf-8"), encoding="utf-8")

        # Create dummy PDF file corresponding to fixture prediction document
        pdf_dir = temp_path / "raw_pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = pdf_dir / "factual_report_2026_001.pdf"
        dummy_pdf_content = b"%PDF-1.4 dummy pdf payload for unit testing evolus client component"
        pdf_file.write_bytes(dummy_pdf_content)

        call_counter = [0]
        received_requests: list[httpx.Request] = []

        def default_mock_handler(req: httpx.Request) -> httpx.Response:
            call_counter[0] += 1
            received_requests.append(req)
            return httpx.Response(
                200,
                json={
                    "tool": "doc-structure",
                    "output": '{"structure": "verified"}',
                    "totalTokenCount": None,
                    "creditsConsumed": 1.5,
                    "sourceText": None,
                    "truncated": False,
                },
            )

        client = httpx.Client(
            base_url="https://api.evolus.ai",
            transport=httpx.MockTransport(default_mock_handler),
            headers={"X-ApiKey": "mock_key_val", "X-Application-Id": "mock_app_val"},
        )

        doc_pred = "FIXTURE:DOC-2026-001"
        doc_truth = "FIXTURE:DOC-2026-002"

        # STEP 2: Execute Case 1 - Success execution, raw file write, usage append | takes: mock transport | gives: case 1 result
        try:
            res1 = run_tool(doc_pred, "doc-structure", {"format": "json", "content": "data"}, client=client, data_dir=temp_path)
            raw_file = Path(res1["raw_path"])
            if not raw_file.exists():
                raise AssertionError(f"Raw response file was not created at {raw_file}")
            raw_data = json.loads(raw_file.read_text(encoding="utf-8"))
            if "meta" not in raw_data or "response" not in raw_data:
                raise AssertionError("Saved JSON missing meta or response top-level keys")
            if raw_data["meta"]["label"] != "GENERATED_BY_EVOLUS_NOT_VERIFIED":
                raise AssertionError("Saved JSON meta label mismatch")
            if raw_data["response"]["output"] != '{"structure": "verified"}':
                raise AssertionError("Saved JSON response output was modified")

            usage_file = temp_path / "processed" / "evolus_raw" / "_usage.jsonl"
            if not usage_file.exists():
                raise AssertionError("Usage log file _usage.jsonl was not created")
            usage_lines = [json.loads(line) for line in usage_file.read_text(encoding="utf-8").splitlines() if line.strip()]
            if len(usage_lines) != 1:
                raise AssertionError(f"Expected 1 usage line, found {len(usage_lines)}")
            if usage_lines[0]["document_id"] != doc_pred or usage_lines[0]["status"] != 200:
                raise AssertionError("Usage log entry fields mismatch")
            print("case 1 (success): PASS")
        except Exception as exc:
            print(f"case 1 (success): FAIL ({exc})")
            sys.exit(1)

        # STEP 3: Execute Case 2 - Cache hit on second call, force bypass | takes: existing raw cache | gives: case 2 result
        try:
            calls_before_cache = call_counter[0]
            res2 = run_tool(doc_pred, "doc-structure", {"format": "json", "content": "data"}, client=client, data_dir=temp_path, force=False)
            if call_counter[0] != calls_before_cache:
                raise AssertionError("Second call made unexpected HTTP requests instead of cache hit")
            if not res2["cached"]:
                raise AssertionError("Result dictionary did not indicate cached=True")

            calls_before_force = call_counter[0]
            res3 = run_tool(doc_pred, "doc-structure", {"format": "json", "content": "data"}, client=client, data_dir=temp_path, force=True)
            if call_counter[0] != calls_before_force + 1:
                raise AssertionError("Force call did not issue exactly one HTTP request")
            if res3["cached"]:
                raise AssertionError("Force call incorrectly marked cached=True")
            print("case 2 (cache): PASS")
        except Exception as exc:
            print(f"case 2 (cache): FAIL ({exc})")
            sys.exit(1)

        # STEP 4: Execute Case 3 - Truth-split document leakage rejection | takes: truth fixture | gives: case 3 result
        try:
            calls_before_truth = call_counter[0]
            truth_rejected = False
            try:
                run_tool(doc_truth, "doc-structure", {}, client=client, data_dir=temp_path)
            except ComponentError as err:
                if err.component == "C3" and err.step == "leakage" and "refusing to send a non-prediction document to Evolus" in err.message:
                    truth_rejected = True
            if not truth_rejected:
                raise AssertionError("Truth document did not trigger expected ComponentError('C3', 'leakage', ...)")
            if call_counter[0] != calls_before_truth:
                raise AssertionError("Truth document rejection issued unauthorized HTTP calls")
            print("case 3 (truth refusal): PASS")
        except Exception as exc:
            print(f"case 3 (truth refusal): FAIL ({exc})")
            sys.exit(1)

        # STEP 5: Execute Case 4 - Oversize file refusal via MAX_FILE_MB monkeypatch | takes: test pdf | gives: case 4 result
        try:
            global MAX_FILE_MB
            orig_max = MAX_FILE_MB
            MAX_FILE_MB = 0
            calls_before_oversize = call_counter[0]
            oversize_rejected = False
            try:
                run_tool(doc_pred, "doc-structure", {}, client=client, data_dir=temp_path, force=True)
            except ComponentError as err:
                if err.component == "C3" and err.step == "file_size":
                    oversize_rejected = True
            finally:
                MAX_FILE_MB = orig_max

            if not oversize_rejected:
                raise AssertionError("Oversize file did not trigger expected ComponentError('C3', 'file_size', ...)")
            if call_counter[0] != calls_before_oversize:
                raise AssertionError("Oversize file rejection issued unauthorized HTTP calls")
            print("case 4 (oversize file): PASS")
        except Exception as exc:
            print(f"case 4 (oversize file): FAIL ({exc})")
            sys.exit(1)

        # STEP 6: Execute Case 5 - Reject base URL with path component | takes: invalid URLs | gives: case 5 result
        try:
            bad_url_rejected = False
            try:
                _validate_base_url("https://api.evolus.ai/api/v1")
            except ComponentError as err:
                if err.component == "C3" and err.step == "config" and "EVOLUS_BASE_URL must be the host only" in err.message:
                    bad_url_rejected = True
            if not bad_url_rejected:
                raise AssertionError("_validate_base_url failed to reject URL with path component")

            bad_client_rejected = False
            bad_client = httpx.Client(base_url="https://api.evolus.ai/api/v1", transport=httpx.MockTransport(default_mock_handler))
            try:
                run_tool(doc_pred, "doc-structure", {}, client=bad_client, data_dir=temp_path, force=True)
            except ComponentError as err:
                if err.component == "C3" and err.step == "config" and "EVOLUS_BASE_URL must be the host only" in err.message:
                    bad_client_rejected = True
            if not bad_client_rejected:
                raise AssertionError("run_tool with bad client base_url path failed to raise ComponentError")
            print("case 5 (base URL path): PASS")
        except Exception as exc:
            print(f"case 5 (base URL path): FAIL ({exc})")
            sys.exit(1)

        # STEP 7: Execute Case 6 - 402 and 422 error handling with error codes | takes: mock error responses | gives: case 6 result
        try:
            with tempfile.TemporaryDirectory() as err_td:
                err_path = Path(err_td).resolve()
                (err_path / "manifest.json").write_text(fixture_manifest_file.read_text(encoding="utf-8"), encoding="utf-8")
                err_pdf_dir = err_path / "raw_pdfs"
                err_pdf_dir.mkdir(parents=True, exist_ok=True)
                (err_pdf_dir / "factual_report_2026_001.pdf").write_bytes(dummy_pdf_content)

                # Test 402 Credits Exhausted
                def handler_402(req: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        402,
                        json={
                            "title": "Credits Exhausted",
                            "status": 402,
                            "detail": "No credits remaining in wallet",
                            "errorCode": "CREDITS_EXHAUSTED",
                            "traceId": "trace-402-abc",
                        },
                    )

                client_402 = httpx.Client(base_url="https://api.evolus.ai", transport=httpx.MockTransport(handler_402))
                err_402_raised = False
                try:
                    run_tool(doc_pred, "doc-structure", {}, client=client_402, data_dir=err_path, force=True)
                except ComponentError as err:
                    if "402" in err.message and "CREDITS_EXHAUSTED" in err.message and "trace-402-abc" in err.message:
                        err_402_raised = True
                if not err_402_raised:
                    raise AssertionError("402 response did not raise ComponentError with expected errorCode and traceId")

                # Test 422 Unprocessable Entity
                def handler_422(req: httpx.Request) -> httpx.Response:
                    return httpx.Response(
                        422,
                        json={
                            "title": "Unreadable File",
                            "status": 422,
                            "detail": "Failed to parse document stream",
                            "errorCode": "UNSUPPORTED_MEDIA",
                            "traceId": "trace-422-xyz",
                        },
                    )

                client_422 = httpx.Client(base_url="https://api.evolus.ai", transport=httpx.MockTransport(handler_422))
                err_422_raised = False
                try:
                    run_tool(doc_pred, "doc-structure", {}, client=client_422, data_dir=err_path, force=True)
                except ComponentError as err:
                    if "422" in err.message and "UNSUPPORTED_MEDIA" in err.message and "trace-422-xyz" in err.message:
                        err_422_raised = True
                if not err_422_raised:
                    raise AssertionError("422 response did not raise ComponentError with expected errorCode and traceId")

                raw_dir_check = err_path / "processed" / "evolus_raw"
                if raw_dir_check.exists():
                    saved_files = list(raw_dir_check.glob("*.json"))
                    if saved_files:
                        raise AssertionError(f"Expected nothing saved on HTTP error, but found: {saved_files}")

            print("case 6 (402 and 422 errors): PASS")
        except Exception as exc:
            print(f"case 6 (402 and 422 errors): FAIL ({exc})")
            sys.exit(1)

        # STEP 8: Execute Case 7 - 429 rate limit retry with sleep patch | takes: mock 429 then 200 | gives: case 7 result
        try:
            attempts_429 = [0]

            def handler_429(req: httpx.Request) -> httpx.Response:
                attempts_429[0] += 1
                if attempts_429[0] == 1:
                    return httpx.Response(
                        429,
                        headers={"Retry-After": "4"},
                        json={
                            "title": "Rate Limit",
                            "status": 429,
                            "detail": "Rate limit exceeded",
                            "errorCode": "RATE_LIMITED",
                            "traceId": "trace-429-111",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "tool": "summarize",
                        "output": "- Key summary bullet point",
                        "totalTokenCount": None,
                        "creditsConsumed": 0.8,
                        "sourceText": None,
                        "truncated": False,
                    },
                )

            client_429 = httpx.Client(base_url="https://api.evolus.ai", transport=httpx.MockTransport(handler_429))
            slept_intervals: list[float] = []
            orig_sleep = time.sleep
            try:
                time.sleep = lambda s: slept_intervals.append(s)
                res7 = summarize_document(doc_pred, client=client_429, data_dir=temp_path, force=True)
            finally:
                time.sleep = orig_sleep

            if not res7["ok"]:
                raise AssertionError("Retry after 429 did not return ok=True")
            if len(slept_intervals) != 1 or slept_intervals[0] != 4.0:
                raise AssertionError(f"Expected sleep of 4.0s, got: {slept_intervals}")
            if attempts_429[0] != 2:
                raise AssertionError(f"Expected exactly 2 attempts, got {attempts_429[0]}")
            print("case 7 (429 retry): PASS")
        except Exception as exc:
            print(f"case 7 (429 retry): FAIL ({exc})")
            sys.exit(1)

        # STEP 9: Execute Case 8 - Auth headers delivery and trace secrecy | takes: mock client with secrets | gives: case 8 result
        try:
            secret_key = "super_secret_evolus_api_key_99999"
            secret_app = "super_secret_evolus_app_id_88888"
            captured_hdrs: dict[str, Optional[str]] = {}

            def handler_auth(req: httpx.Request) -> httpx.Response:
                captured_hdrs["X-ApiKey"] = req.headers.get("x-apikey")
                captured_hdrs["X-Application-Id"] = req.headers.get("x-application-id")
                return httpx.Response(
                    200,
                    json={
                        "tool": "doc-structure",
                        "output": "{}",
                        "totalTokenCount": None,
                        "creditsConsumed": 0.2,
                        "sourceText": None,
                        "truncated": False,
                    },
                )

            client_auth = httpx.Client(
                base_url="https://api.evolus.ai",
                transport=httpx.MockTransport(handler_auth),
                headers={"X-ApiKey": secret_key, "X-Application-Id": secret_app},
            )

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                run_tool(doc_pred, "doc-structure", {}, client=client_auth, data_dir=temp_path, force=True)

            captured_stdout = buf.getvalue()

            if captured_hdrs.get("X-ApiKey") != secret_key:
                raise AssertionError("X-ApiKey header was not transmitted to request")
            if captured_hdrs.get("X-Application-Id") != secret_app:
                raise AssertionError("X-Application-Id header was not transmitted to request")
            if secret_key in captured_stdout or secret_app in captured_stdout:
                raise AssertionError("Secret API key or Application ID leaked in stdout / trace output")

            trace_log = temp_path / "trace.log"
            if trace_log.exists():
                trace_content = trace_log.read_text(encoding="utf-8")
                if secret_key in trace_content or secret_app in trace_content:
                    raise AssertionError("Secret leaked in trace.log file")

            print("case 8 (auth headers and trace secrecy): PASS")
        except Exception as exc:
            print(f"case 8 (auth headers and trace secrecy): FAIL ({exc})")
            sys.exit(1)

        print("PASS")

    finally:
        temp_dir.cleanup()


def main() -> None:
    """IN: None (reads command-line arguments from sys.argv).
    OUT: None (executes requested CLI action).
    FAILS WITH: SystemExit on execution error or failure."""
    # STEP 1: Configure CLI parser with options for selftest and live runs | takes: sys.argv | gives: parsed args
    parser = argparse.ArgumentParser(description="Component C3 - Evolus Client")
    parser.add_argument("--selftest", action="store_true", help="Run offline fixture self-test suite")
    parser.add_argument("--live", type=str, default=None, help="Document ID for live API execution")
    parser.add_argument(
        "--tool",
        type=str,
        choices=["extract", "summarize", "both"],
        default="both",
        help="Target tool: extract, summarize, or both (default: both)",
    )
    parser.add_argument("--force", action="store_true", help="Bypass cache and force HTTP execution")

    args = parser.parse_args()

    # STEP 2: Dispatch to self-test runner if requested | takes: args.selftest | gives: selftest outcome
    if args.selftest:
        run_selftest()
        return

    # STEP 3: Dispatch live tool execution | takes: args.live, args.tool, args.force | gives: console results
    if args.live:
        doc_id = args.live
        tools_to_run = ["extract", "summarize"] if args.tool == "both" else [args.tool]
        for t in tools_to_run:
            try:
                if t == "extract":
                    res = extract_document(doc_id, force=args.force)
                    saved_tool_name = "doc-structure"
                else:
                    res = summarize_document(doc_id, force=args.force)
                    saved_tool_name = "summarize"

                print(f"=== Tool: {t} ===")
                print(f"Result: {json.dumps(res, indent=2)}")
                saved = load_saved(doc_id, saved_tool_name)
                output_str = ""
                if saved and isinstance(saved, dict):
                    output_str = str(saved.get("response", {}).get("output", "") or "")
                print(f"Output (first 300 chars):\n{output_str[:300]}\n")
            except Exception as exc:
                print(f"Error running tool '{t}' on document '{doc_id}': {exc}", file=sys.stderr)
                sys.exit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()

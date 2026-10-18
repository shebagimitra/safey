# ============================================================
# COMPONENT: C7 / C11a - Backend REST API & Packaging
# PURPOSE: FastAPI application serving cases, evidence, analysis orchestrations, run logs, and official conclusions with replay-only support.
# INPUT: HTTP REST requests (GET, POST), route parameters, JSON request payloads, and local data directory files.
# OUTPUT: JSON API responses (RunLog, Passage, Component counts, Health, Reveal) and static files.
# CALLED BY: Frontend UI, citation drawer, evaluation scripts, and test suite      CALLS: evidence.store, evidence.pack_builder, model.harness, common.config, common.trace, model.llm_client
# IF IT BREAKS, CHECK: SQLite database existence, DATA_DIR path resolution, CORS origin rules, LLM server availability, ALLOW_LIVE setting.
# ============================================================

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from common.config import get_data_dir, get_settings
from common.models import Passage, RunLog
from common.trace import ComponentError, trace
from evidence.pack_builder import build_pack
from evidence.store import get_passage, load_passages, search
from model.harness import run_analysis
from model.llm_client import health_check
from process.evolus_client import load_saved


def _parse_allow_live(env_val: Optional[str]) -> bool:
    """IN: env_val (Optional[str]) from environment.
    OUT: bool indicating whether live mode is allowed.
    FAILS WITH: Never fails."""
    # STEP 1: Parse truthiness case-insensitively with default True | takes: env_val | gives: bool
    if env_val is None:
        return True
    return env_val.strip().lower() in ("1", "true", "yes")


# Read env ALLOW_LIVE once at startup: truthy only for "1","true","yes" (case-insensitive). CODE DEFAULT = true
ALLOW_LIVE: bool = _parse_allow_live(os.environ.get("ALLOW_LIVE"))

# Standard aircraft component identifiers displayed in the frontend schematic
VALID_COMPONENTS: list[str] = [
    "engine",
    "landing_gear",
    "hydraulic_system",
    "wing_flaps",
    "fuel_system",
]


class AnalyzeRequest(BaseModel):
    """Request payload schema for orchestrating an analysis run."""
    model_config = ConfigDict(extra="ignore")

    case_id: str = Field(description="Target aviation case identifier")
    component_id: str = Field(description="Aircraft component identifier")
    question: str = Field(description="Analytical investigation query")
    mode: str = Field(default="A", description="Reasoning mode: '0', 'A', 'B', 'C', or 'live'")
    token_budget: int = Field(default=6000, description="Token budget limit for evidence pack")
    use_saved: bool = Field(default=False, description="Reuse existing successful RunLog if present")


def _get_active_data_dir() -> Path:
    """IN: None (reads DATA_DIR from environment or common configuration).
    OUT: Resolved Path to local data directory.
    FAILS WITH: ComponentError on path resolution failure."""
    # STEP 1: Resolve data directory path | takes: os.environ, get_data_dir | gives: resolved Path
    try:
        return get_data_dir()
    except Exception as exc:
        raise ComponentError("C7", "resolve_data_dir", f"Failed to resolve data directory: {exc}") from exc


def _get_db_path() -> Path:
    """IN: None.
    OUT: Path to active SQLite evidence database.
    FAILS WITH: ComponentError if database file cannot be determined."""
    # STEP 1: Inspect environment overrides and standard database candidate paths | takes: None | gives: Path
    data_dir = _get_active_data_dir()
    env_db = os.environ.get("EVIDENCE_DB_PATH")
    if env_db:
        return Path(env_db)

    candidates = [
        data_dir / "db" / "evidence.db",
        data_dir / "db" / "passages.db",
        data_dir / "passages.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return data_dir / "db" / "passages.db"


def init_db_if_missing() -> None:
    """IN: None.
    OUT: None.
    FAILS WITH: ComponentError if database build fails."""
    # STEP 1: Check existence of SQLite evidence database | takes: _get_db_path | gives: db_path
    db_path = _get_db_path()
    if db_path.exists():
        return

    # STEP 2: Locate source passages JSONL file | takes: _get_active_data_dir | gives: passages_path
    data_dir = _get_active_data_dir()
    passages_path = data_dir / "processed" / "passages.jsonl"
    if not passages_path.exists():
        for cand in [data_dir / "passages.jsonl", Path("contracts/fixtures/passages.jsonl")]:
            if cand.exists():
                passages_path = cand
                break

    if not passages_path.exists():
        trace("C11a", "Cannot build database on startup: passages.jsonl not found", db_path=str(db_path))
        return

    # STEP 3: Load passages and construct database via evidence.store | takes: passages_path, db_path | gives: loaded_count
    trace("C11a", "Evidence database missing on startup, building from passages", passages_path=str(passages_path), db_path=str(db_path))
    try:
        loaded_count = load_passages(passages_path, db_path)
        trace("C11a", "Evidence database built successfully on startup", passage_count=loaded_count, db_path=str(db_path))
    except Exception as exc:
        trace("C11a", "Failed to build evidence database on startup", error=str(exc))
        raise ComponentError("C11a", "db_startup_init", f"Failed building database at {db_path} from {passages_path}: {exc}") from exc


def _resolve_pdf_url(passage: Passage, manifest_path: Optional[Path] = None) -> tuple[Optional[int], str]:
    """IN: Validated Passage model and optional manifest Path.
    OUT: Tuple of (page_number: Optional[int], pdf_url: str).
    FAILS WITH: Never fails; falls back to default document URL."""
    # STEP 1: Search document manifest for parent PDF filename | takes: passage, manifest_path | gives: pdf_filename
    page_num = passage.page_number
    pdf_filename = f"{passage.document_id}.pdf"

    target_manifest = manifest_path or (_get_active_data_dir() / "manifest.json")
    if not target_manifest.exists():
        fixture_manifest = Path("contracts/fixtures/manifest.json")
        if fixture_manifest.exists():
            target_manifest = fixture_manifest

    if target_manifest.exists():
        try:
            manifest_content = json.loads(target_manifest.read_text(encoding="utf-8"))
            if isinstance(manifest_content, list):
                for doc in manifest_content:
                    if doc.get("document_id") == passage.document_id:
                        file_path_raw = doc.get("file_path", "")
                        clean_path = file_path_raw.replace("FIXTURE:", "")
                        if clean_path:
                            pdf_filename = Path(clean_path).name
                        break
        except Exception as exc:
            trace("C7", "Manifest lookup warning", error=str(exc))

    # STEP 2: Format static PDF citation link with page anchor | takes: pdf_filename, page_num | gives: pdf_url
    if page_num is not None:
        pdf_url = f"/pdfs/{pdf_filename}#page={page_num}"
    else:
        pdf_url = f"/pdfs/{pdf_filename}"

    return page_num, pdf_url


def _has_run_for_case(case_id: str, runs_dir: Path) -> bool:
    """IN: case_id (str) and runs_dir (Path).
    OUT: True if at least one RunLog exists for this case without error, False otherwise.
    FAILS WITH: Never fails; returns False on I/O error."""
    # STEP 1: Scan runs directory and match run logs without error to case_id | takes: case_id, runs_dir | gives: bool match
    if not runs_dir.exists():
        return False

    clean_case = re.sub(r"[^A-Za-z0-9_-]", "_", case_id)
    packs_dir = _get_active_data_dir() / "packs"

    for run_file in runs_dir.glob("*.json"):
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue

            # HARD RULE: A valid prediction run must have error == None
            if data.get("error") is not None:
                continue

            # Check assessment case_id
            assessment = data.get("assessment")
            if isinstance(assessment, dict) and assessment.get("case_id") == case_id:
                return True

            # Check top-level case_id if present
            if data.get("case_id") == case_id:
                return True

            # Check pack_id naming convention or pack file contents
            pack_id = data.get("pack_id", "")
            if isinstance(pack_id, str):
                if f"_{clean_case}_" in pack_id or pack_id.startswith(f"pack_{clean_case}_"):
                    return True
                if case_id in pack_id:
                    return True
                if packs_dir.exists():
                    p_file = packs_dir / f"{pack_id}.json"
                    if p_file.exists():
                        p_data = json.loads(p_file.read_text(encoding="utf-8"))
                        if isinstance(p_data, dict) and p_data.get("case_id") == case_id:
                            return True
        except Exception:
            continue

    return False


# Initialize FastAPI application
app = FastAPI(
    title="Antigravity Investigation API",
    version="1.0.0",
    description="Component C7 Backend REST API for aircraft investigation analysis.",
)

# Configure CORS restricted strictly to localhost development
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:8000",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount contracts/fixtures read-only at /fixtures
_fixtures_dir = Path("contracts/fixtures")
if _fixtures_dir.exists():
    app.mount("/fixtures", StaticFiles(directory=str(_fixtures_dir)), name="fixtures")

# Mount frontend/ at /frontend (so /frontend/schematic_demo.html loads ./schematic.js and ./styles.css)
_frontend_dir = Path("frontend")
if _frontend_dir.exists():
    app.mount("/frontend", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")


@app.exception_handler(ComponentError)
async def component_error_handler(request: Request, exc: ComponentError) -> JSONResponse:
    """IN: FastAPI Request and ComponentError instance.
    OUT: JSONResponse formatted with {error: {component, step, message}}.
    FAILS WITH: Never fails."""
    # STEP 1: Format standardized JSON error response | takes: exc | gives: JSONResponse
    trace("C7", "ComponentError caught in API handler", component=exc.component, step=exc.step, error=exc.message)
    status_code = 404 if "not found" in exc.message.lower() else 400
    return JSONResponse(
        status_code=status_code,
        content={"error": {"component": exc.component, "step": exc.step, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """IN: FastAPI Request and RequestValidationError instance.
    OUT: JSONResponse formatted with {error: {component, step, message}}.
    FAILS WITH: Never fails."""
    # STEP 1: Wrap validation failures in standard diagnostic schema | takes: exc | gives: JSONResponse
    trace("C7", "Request validation failed", error_count=len(exc.errors()))
    return JSONResponse(
        status_code=422,
        content={"error": {"component": "C7", "step": "request_validation", "message": str(exc)}},
    )


@app.on_event("startup")
def startup_event() -> None:
    """IN: None.
    OUT: None.
    FAILS WITH: ComponentError on startup initialization failure."""
    # STEP 1: Verify and build evidence database if missing | takes: None | gives: database ready
    init_db_if_missing()


@app.get("/api/health")
def get_health() -> dict[str, Any]:
    """IN: None (invoked via GET /api/health).
    OUT: Dictionary indicating whether the LLM endpoint is reachable and execution mode.
    FAILS WITH: ComponentError if health check execution fails unexpectedly in live mode."""
    # STEP 1: Check replay-only mode guard | takes: ALLOW_LIVE | gives: replay-only response without LLM call
    if not ALLOW_LIVE:
        trace("C11a", "Health check evaluated in replay-only mode", mode="replay-only")
        return {
            "status": "ok",
            "mode": "replay-only",
        }

    # STEP 2: Probe LLM endpoint reachability via health_check | takes: None | gives: boolean is_reachable
    try:
        is_reachable = health_check()
        trace("C7", "Health check evaluated", reachable=is_reachable)
        return {
            "status": "ok" if is_reachable else "degraded",
            "reachable": is_reachable,
            "llm_reachable": is_reachable,
            "mode": "live",
        }
    except Exception as exc:
        trace("C7", "Health check execution error", error=str(exc))
        raise ComponentError("C7", "health_check", f"Health check probe failed: {exc}") from exc


@app.get("/api/cases")
def get_cases() -> list[dict[str, Any]]:
    """IN: None (invoked via GET /api/cases).
    OUT: List of case summary dictionaries from manifest or store, prediction split only.
    FAILS WITH: ComponentError if manifest or store cannot be accessed."""
    # STEP 1: Read case records from manifest or fallback to database | takes: None | gives: cases list
    data_dir = _get_active_data_dir()
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        fixture_manifest = Path("contracts/fixtures/manifest.json")
        if fixture_manifest.exists():
            manifest_path = fixture_manifest

    cases_map: dict[str, dict[str, Any]] = {}

    if manifest_path.exists():
        try:
            docs = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(docs, list):
                for d in docs:
                    # HARD RULE LEAKAGE GUARD: list only documents whose split is "prediction"
                    if d.get("split") != "prediction":
                        continue
                    cid = d.get("case_id")
                    if not cid:
                        continue
                    if cid not in cases_map:
                        cases_map[cid] = {
                            "case_id": cid,
                            "title": d.get("title", cid),
                            "publisher": d.get("publisher", ""),
                            "document_count": 0,
                            "documents": [],
                        }
                    cases_map[cid]["document_count"] += 1
                    cases_map[cid]["documents"].append(d)
        except Exception as exc:
            raise ComponentError("C7", "get_cases", f"Failed reading manifest at {manifest_path}: {exc}") from exc

    if not cases_map:
        # Fallback to distinct cases present in the store (prediction split only)
        try:
            import sqlite3
            db_path = _get_db_path()
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT DISTINCT case_id FROM passages WHERE split = 'prediction';").fetchall()
                conn.close()
                for r in rows:
                    cid = r["case_id"]
                    cases_map[cid] = {"case_id": cid, "title": cid, "document_count": 0, "documents": []}
        except Exception as exc:
            raise ComponentError("C7", "get_cases", f"Failed querying database for cases: {exc}") from exc

    # STEP 2: Format sorted list of case dictionaries | takes: cases_map | gives: cases list
    result = list(cases_map.values())
    trace("C7", "Retrieved prediction cases list", count=len(result))
    return result


@app.get("/api/cases/{case_id}/components")
def get_case_components(case_id: str) -> dict[str, Any]:
    """IN: case_id (str) path parameter.
    OUT: Dictionary mapping components to passage counts or 'no curated evidence yet'.
    FAILS WITH: ComponentError on database search failure."""
    # STEP 1: Query store passage counts for each component | takes: case_id, VALID_COMPONENTS | gives: counts_dict
    db_path = _get_db_path()
    if not db_path.exists():
        raise ComponentError("C7", "get_case_components", f"Database not found at {db_path}")

    counts_dict: dict[str, Any] = {}
    total_found = 0

    # Collect known components plus any dynamic tags from the database
    target_components = list(VALID_COMPONENTS)
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        tag_rows = conn.execute(
            "SELECT component_tags FROM passages WHERE case_id = ? AND split = 'prediction';",
            (case_id,),
        ).fetchall()
        conn.close()
        for tr in tag_rows:
            raw_tags = tr["component_tags"]
            if raw_tags:
                tag_list = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
                for t in tag_list:
                    clean_t = t.replace("FIXTURE:", "")
                    if clean_t not in target_components:
                        target_components.append(clean_t)
    except Exception as exc:
        trace("C7", "Warning querying component tags", error=str(exc))

    for comp in target_components:
        try:
            # Query prediction passages for component
            passages = search(db_path, query="", case_id=case_id, component_id=comp, limit=10000)
            cnt = len(passages)
            total_found += cnt
            if cnt > 0:
                counts_dict[comp] = cnt
            else:
                counts_dict[comp] = "no curated evidence yet"
        except Exception as exc:
            raise ComponentError("C7", "get_case_components", f"Failed counting passages for '{comp}': {exc}") from exc

    # STEP 2: Assemble composite response matching dict and nested access patterns | takes: counts_dict | gives: response_dict
    trace("C7", "Retrieved component passage counts", case_id=case_id, total_passages=total_found)
    return {
        "case_id": case_id,
        "components": counts_dict,
        **counts_dict,
    }


def _overwrite_documented_findings_roles(run_data: Any) -> Any:
    """IN: run_data (RunLog model or dict).
    OUT: run_data with assessment.documented_findings[].source_role updated from evidence store.
    FAILS WITH: Never fails; defaults missing or failed passages to 'observation'."""
    # STEP 1: Determine database path for passage lookup | takes: None | gives: Path or None
    try:
        db_path = _get_db_path()
        db_exists = db_path.exists()
    except Exception:
        db_path = None
        db_exists = False

    # STEP 2: Extract documented findings list from model or dict | takes: run_data | gives: findings_to_update
    findings_to_update: list[Any] = []
    if isinstance(run_data, dict):
        assessment = run_data.get("assessment")
        if isinstance(assessment, dict):
            findings_to_update = assessment.get("documented_findings") or []
    elif hasattr(run_data, "assessment") and run_data.assessment is not None:
        assessment = run_data.assessment
        if hasattr(assessment, "documented_findings"):
            findings_to_update = getattr(assessment, "documented_findings") or []
        elif isinstance(assessment, dict):
            findings_to_update = assessment.get("documented_findings") or []

    # STEP 3: Lookup cited passages in store and overwrite source_role | takes: findings_to_update, db_path | gives: updated findings
    for finding in findings_to_update:
        if isinstance(finding, dict):
            ev_id = finding.get("evidence_id")
            resolved_role = "observation"
            if ev_id and db_exists and db_path:
                try:
                    passage = get_passage(db_path, ev_id, allow_truth=False)
                    if passage and passage.evidence_role:
                        resolved_role = passage.evidence_role
                except Exception:
                    resolved_role = "observation"
            finding["source_role"] = resolved_role
        elif hasattr(finding, "evidence_id"):
            ev_id = getattr(finding, "evidence_id", None)
            resolved_role = "observation"
            if ev_id and db_exists and db_path:
                try:
                    passage = get_passage(db_path, ev_id, allow_truth=False)
                    if passage and passage.evidence_role:
                        resolved_role = passage.evidence_role
                except Exception:
                    resolved_role = "observation"
            setattr(finding, "source_role", resolved_role)

    trace("C7", "Resolved documented finding source_role", count=len(findings_to_update))
    return run_data


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> Any:
    """IN: AnalyzeRequest containing case_id, component_id, question, mode, token_budget, optional use_saved.
    OUT: Completed RunLog serialized dictionary or model.
    FAILS WITH: ComponentError on pack generation or analysis failure."""
    db_path = _get_db_path()
    trace(
        "C7",
        "Received analysis request",
        case_id=req.case_id,
        component_id=req.component_id,
        mode=req.mode,
        token_budget=req.token_budget,
        use_saved=req.use_saved,
    )

    try:
        # STEP 1: Guard against live execution if use_saved is False in replay-only mode | takes: ALLOW_LIVE, req.use_saved | gives: 403 response
        if not ALLOW_LIVE and not req.use_saved:
            trace("C11a", "Live analysis blocked in replay-only mode", case_id=req.case_id, mode=req.mode)
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "component": "C11a",
                        "step": "replay_only",
                        "message": "Live model calls are disabled in this deployment (replay-only). Select a saved run.",
                    }
                },
            )

        # STEP 2: Build bounded EvidencePack via C5 pack_builder | takes: db_path, req | gives: EvidencePack
        pack = build_pack(
            db_path=db_path,
            case_id=req.case_id,
            component_id=req.component_id,
            question=req.question,
            mode=req.mode,
            token_budget=req.token_budget,
        )
        trace("C7", "Evidence pack assembled", pack_id=pack.pack_id, passage_count=len(pack.passages))

        # STEP 3: Return existing RunLog without error if use_saved is requested | takes: req.use_saved, pack.pack_id | gives: saved RunLog or None
        if req.use_saved:
            runs_dir = _get_active_data_dir() / "runs"
            if runs_dir.exists():
                for run_file in sorted(runs_dir.glob("*.json")):
                    try:
                        saved_data = json.loads(run_file.read_text(encoding="utf-8"))
                        if isinstance(saved_data, dict):
                            if (
                                saved_data.get("pack_id") == pack.pack_id
                                and saved_data.get("run_label") == req.mode
                                and saved_data.get("error") is None
                            ):
                                trace(
                                    "C7",
                                    "Reusing saved RunLog without model call",
                                    run_id=saved_data.get("run_id"),
                                    pack_id=pack.pack_id,
                                    mode=req.mode,
                                )
                                return _overwrite_documented_findings_roles(saved_data)
                    except Exception:
                        continue

        # STEP 4: Guard against live execution in replay-only mode when no saved run exists | takes: ALLOW_LIVE | gives: 403 response
        if not ALLOW_LIVE:
            trace("C11a", "Live analysis blocked in replay-only mode: no saved run found", case_id=req.case_id, mode=req.mode)
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "component": "C11a",
                        "step": "replay_only",
                        "message": "Live model calls are disabled in this deployment (replay-only). Select a saved run.",
                    }
                },
            )

        # STEP 5: Execute analysis harness via C6 | takes: pack, req.mode | gives: RunLog
        run_log = run_analysis(pack=pack, run_label=req.mode)
        trace("C7", "Analysis completed", run_id=run_log.run_id, model=run_log.model_name)

        # STEP 6: Return completed RunLog | takes: run_log | gives: serialized RunLog response
        return _overwrite_documented_findings_roles(run_log)
    except ComponentError as ce:
        trace("C7", "Analysis execution failed with ComponentError", component=ce.component, step=ce.step, error=ce.message)
        return JSONResponse(
            status_code=400,
            content={"error": {"component": ce.component, "step": ce.step, "message": ce.message}},
        )
    except Exception as exc:
        trace("C7", "Analysis execution unexpected failure", error=str(exc))
        return JSONResponse(
            status_code=500,
            content={"error": {"component": "C7", "step": "analyze", "message": str(exc)}},
        )


@app.get("/api/evidence/{passage_id}")
def get_evidence(passage_id: str) -> dict[str, Any]:
    """IN: passage_id (str) path parameter.
    OUT: Passage dictionary augmented with page_number and static pdf_url.
    FAILS WITH: ComponentError if passage not found or leakage guard blocks."""
    # STEP 1: Query passage by identifier from store | takes: passage_id | gives: Passage model
    db_path = _get_db_path()
    passage = get_passage(db_path, passage_id, allow_truth=False)
    if passage is None:
        trace("C7", "Passage not found or suppressed by leakage guard", passage_id=passage_id)
        return JSONResponse(
            status_code=404,
            content={"error": {"component": "C7", "step": "get_evidence", "message": f"Passage '{passage_id}' not found"}},
        )

    # STEP 2: Resolve PDF citation page link and assemble payload | takes: passage | gives: response_payload
    page_number, pdf_url = _resolve_pdf_url(passage)
    data = passage.model_dump()
    data["page_number"] = page_number
    data["pdf_url"] = pdf_url

    trace("C7", "Evidence passage retrieved", passage_id=passage_id, page=page_number, pdf_url=pdf_url)
    return data


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    """IN: run_id (str) path parameter.
    OUT: Serialized RunLog dictionary.
    FAILS WITH: ComponentError if run log file is missing or invalid."""
    # STEP 1: Locate run log file in data/runs/ directory | takes: run_id | gives: run_file Path
    clean_id = run_id[:-5] if run_id.endswith(".json") else run_id
    runs_dir = _get_active_data_dir() / "runs"
    run_file = runs_dir / f"{clean_id}.json"

    if not run_file.exists():
        trace("C7", "Run log not found", run_id=run_id, path=str(run_file))
        return JSONResponse(
            status_code=404,
            content={"error": {"component": "C7", "step": "get_run", "message": f"RunLog '{run_id}' not found"}},
        )

    # STEP 2: Read and deserialize RunLog JSON payload | takes: run_file | gives: run_dict
    try:
        run_dict = json.loads(run_file.read_text(encoding="utf-8"))
        run_dict = _overwrite_documented_findings_roles(run_dict)
        trace("C7", "RunLog retrieved", run_id=clean_id)
        return run_dict
    except Exception as exc:
        raise ComponentError("C7", "get_run", f"Failed reading RunLog at {run_file}: {exc}") from exc


@app.get("/api/runs")
def list_runs(case_id: Optional[str] = None) -> list[dict[str, Any]]:
    """IN: Optional case_id query parameter.
    OUT: List of RunLog dictionaries matching case_id filter.
    FAILS WITH: ComponentError on directory reading failure."""
    # STEP 1: Scan runs directory and filter by case_id if provided | takes: case_id | gives: matching_runs
    runs_dir = _get_active_data_dir() / "runs"
    if not runs_dir.exists():
        return []

    matching_runs: list[dict[str, Any]] = []
    clean_case = re.sub(r"[^A-Za-z0-9_-]", "_", case_id) if case_id else None
    packs_dir = _get_active_data_dir() / "packs"

    for run_file in sorted(runs_dir.glob("*.json")):
        try:
            run_dict = json.loads(run_file.read_text(encoding="utf-8"))
            if not isinstance(run_dict, dict):
                continue

            if case_id is not None:
                matches = False
                assessment = run_dict.get("assessment")
                if isinstance(assessment, dict) and assessment.get("case_id") == case_id:
                    matches = True
                elif run_dict.get("case_id") == case_id:
                    matches = True
                else:
                    pack_id = run_dict.get("pack_id", "")
                    if isinstance(pack_id, str):
                        if clean_case and (f"_{clean_case}_" in pack_id or pack_id.startswith(f"pack_{clean_case}_")):
                            matches = True
                        elif case_id in pack_id:
                            matches = True
                        elif packs_dir.exists():
                            p_file = packs_dir / f"{pack_id}.json"
                            if p_file.exists():
                                p_data = json.loads(p_file.read_text(encoding="utf-8"))
                                if isinstance(p_data, dict) and p_data.get("case_id") == case_id:
                                    matches = True
                if not matches:
                    continue

            matching_runs.append(_overwrite_documented_findings_roles(run_dict))
        except Exception:
            continue

    trace("C7", "Listed run logs", total=len(matching_runs), case_filter=case_id)
    return matching_runs


@app.get("/api/reveal/{case_id}")
def reveal_conclusion(case_id: str) -> dict[str, Any]:
    """IN: case_id (str) path parameter.
    OUT: Benchmark dictionary containing official conclusion text.
    FAILS WITH: ComponentError (403) if withheld-conclusion leakage rule is violated."""
    # WITHHELD-CONCLUSION LEAKAGE GUARD RULE:
    # In accordance with benchmark integrity specifications, official ground-truth findings
    # in data/truth/ must remain strictly withheld and isolated until after a valid prediction
    # run without error has completed and been logged to data/runs/. This endpoint is the only
    # route in the entire application permitted to access data/truth/, and must refuse with HTTP 403
    # if no prior prediction RunLog without error exists for the requested case_id.

    runs_dir = _get_active_data_dir() / "runs"

    # STEP 1: Verify prediction run without error exists before permitting ground-truth reveal | takes: case_id, runs_dir | gives: bool authorization
    if not _has_run_for_case(case_id, runs_dir):
        trace("C7", "Reveal refused: prior successful prediction run missing", case_id=case_id)
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "component": "C7",
                    "step": "reveal_leakage_guard",
                    "message": f"Refused: Official conclusion withheld. At least one successful prediction run for case '{case_id}' must exist in data/runs/ before reveal.",
                }
            },
        )

    # STEP 2: Load official conclusion benchmark from data/truth/ | takes: case_id | gives: benchmark_dict
    data_dir = _get_active_data_dir()
    clean_case = re.sub(r"[^A-Za-z0-9_-]", "_", case_id)
    candidate_paths = [
        data_dir / "truth" / f"benchmark_{case_id}.json",
        data_dir / "truth" / f"benchmark_{clean_case}.json",
        data_dir / "truth" / "benchmark.json",
        data_dir / "withheld_truth" / "ground_truth.json",
        Path("contracts/fixtures/benchmark.json"),
    ]

    benchmark_data: Optional[dict[str, Any]] = None
    for cp in candidate_paths:
        if cp.exists():
            try:
                raw_bench = json.loads(cp.read_text(encoding="utf-8"))
                if isinstance(raw_bench, dict):
                    # If file has case_id, verify match unless generic benchmark.json
                    if raw_bench.get("case_id") in (case_id, None) or "benchmark_" not in cp.name:
                        benchmark_data = raw_bench
                        break
            except Exception:
                continue

    if benchmark_data is None:
        trace("C7", "Benchmark conclusion not found", case_id=case_id)
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "component": "C7",
                    "step": "reveal_conclusion",
                    "message": f"Official conclusion benchmark not found for case '{case_id}'",
                }
            },
        )

    conclusion_text = (
        benchmark_data.get("official_conclusion_text")
        or benchmark_data.get("official_probable_cause")
        or benchmark_data.get("conclusion", "")
    )

    trace("C7", "Official conclusion revealed successfully", case_id=case_id)
    return {
        "case_id": case_id,
        "official_conclusion": conclusion_text,
        "official_conclusion_text": conclusion_text,
        "questions": benchmark_data.get("questions", []),
        "benchmark": benchmark_data,
    }


# ============================================================
# COMPONENT C3b: Evolus Document Overview & Number Checking
# ============================================================

_NUMBER_PATTERN: re.Pattern[str] = re.compile(r"\b(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\b")


def extract_number_tokens(text: str) -> list[str]:
    """IN: text (str) containing potential numerical expressions.
    OUT: List of normalized numeric token strings without thousands commas.
    FAILS WITH: Never fails; returns empty list on invalid input."""
    # STEP 1: Search for digit groups matching integer, floating point, and comma-formatted patterns | takes: text | gives: tokens
    if not text:
        return []
    results: list[str] = []
    for m in _NUMBER_PATTERN.finditer(str(text)):
        token = m.group(0)
        start, end = m.span()
        # STEP 2: Ignore single-digit tokens 0-9 that are part of words | takes: token, spans | gives: filtered token
        if len(token) == 1 and token.isdigit():
            if start >= 2 and text[start - 1] == "-" and text[start - 2].isalpha():
                continue
            if end + 1 < len(text) and text[end] == "-" and text[end + 1].isalpha():
                continue
        # STEP 3: Normalize numeric token by removing thousands separators | takes: token | gives: normalized string
        norm = token.replace(",", "")
        results.append(norm)
    return results


def is_number_in_text(token: str, text: str) -> bool:
    """IN: token (str) numeric token, text (str) source document passage text.
    OUT: bool indicating whether token appears as a whole token in text.
    FAILS WITH: Never fails; returns False on empty token/text."""
    # STEP 1: Check inputs and normalize passage text by stripping thousands commas | takes: token, text | gives: norm_text
    # NOTE: This check proves a figure appears in the source text, NOT that the sentence uses it correctly.
    if not token or not text:
        return False
    norm_text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    # STEP 2: Construct whole-token boundary pattern avoiding adjacent digits, letters, or decimal points | takes: token | gives: regex
    pattern = re.compile(r"(?<![A-Za-z0-9.])" + re.escape(token) + r"(?![A-Za-z0-9.])")
    # STEP 3: Perform search across raw and normalized texts | takes: pattern, text, norm_text | gives: match bool
    return bool(pattern.search(text) or pattern.search(norm_text))


def check_facts_numbers(data: Any, concatenated_text: str) -> tuple[int, int, list[str]]:
    """IN: data (parsed JSON facts structure), concatenated_text (str).
    OUT: Tuple of (numbers_checked: int, numbers_found: int, missing: list[str]).
    FAILS WITH: Never fails; returns zero counts on empty or invalid structure."""
    # STEP 1: Traverse JSON facts tree and collect all string/number leaves containing digits | takes: data | gives: leaves
    # NOTE: This check proves a figure appears in the source text, NOT that the sentence uses it correctly.
    leaves: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, (int, float)):
            leaves.append(str(node))
        elif isinstance(node, str) and any(ch.isdigit() for ch in node):
            leaves.append(node)

    _walk(data)

    # STEP 2: Extract normalized numeric tokens and check against source passages | takes: leaves, concatenated_text | gives: counts and missing list
    numbers_checked = 0
    numbers_found = 0
    missing: list[str] = []
    for leaf in leaves:
        tokens = extract_number_tokens(leaf)
        for t in tokens:
            numbers_checked += 1
            if is_number_in_text(t, concatenated_text):
                numbers_found += 1
            else:
                if t not in missing:
                    missing.append(t)

    return numbers_checked, numbers_found, missing


@app.get("/api/evolus/{document_id}")
def get_evolus_overview(document_id: str) -> dict[str, Any]:
    """IN: document_id (str) path parameter.
    OUT: Dictionary containing document overview, summary bullets with verified numbers, and facts JSON.
    FAILS WITH: ComponentError (or 404 JSONResponse) if document unknown, non-prediction, or unpopulated."""
    trace("C3b", "Received Evolus overview request", document_id=document_id)

    # STEP 1: Look up document in manifest.json with strict prediction split leakage guard | takes: document_id | gives: doc metadata
    data_dir = _get_active_data_dir()
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        fixture_manifest = Path("contracts/fixtures/manifest.json")
        if fixture_manifest.exists():
            manifest_path = fixture_manifest

    doc_entry: Optional[dict[str, Any]] = None
    if manifest_path.exists():
        try:
            docs = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(docs, list):
                for d in docs:
                    if d.get("document_id") == document_id:
                        doc_entry = d
                        break
        except Exception as exc:
            trace("C3b", "Manifest read error", error=str(exc))

    if doc_entry is None or doc_entry.get("split") != "prediction":
        trace("C3b", "Document lookup rejected: unknown or non-prediction", document_id=document_id)
        return JSONResponse(
            status_code=404,
            content={"error": {"component": "C3b", "step": "manifest_lookup", "message": f"Document '{document_id}' not found"}},
        )

    # STEP 2: Load saved tool outputs (REPLAY-ONLY; never invoke network tools) | takes: document_id | gives: summarize_saved, struct_saved
    summarize_saved = load_saved(document_id, "summarize", data_dir=data_dir)
    struct_saved = load_saved(document_id, "doc-structure", data_dir=data_dir)

    if summarize_saved is None and struct_saved is None:
        trace("C3b", "No saved Evolus output found", document_id=document_id)
        return JSONResponse(
            status_code=404,
            content={"error": {"component": "C3b", "step": "load", "message": "no saved Evolus output"}},
        )

    # STEP 3: Load prediction passages for number verification | takes: document_id, data_dir | gives: concatenated_text
    passages_file = data_dir / "processed" / "passages.jsonl"
    if not passages_file.exists():
        for cand in [data_dir / "passages.jsonl", Path("contracts/fixtures/passages.jsonl")]:
            if cand.exists():
                passages_file = cand
                break

    matching_texts: list[str] = []
    if passages_file.exists():
        try:
            with open(passages_file, "r", encoding="utf-8") as pf:
                for line in pf:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    try:
                        p_rec = json.loads(line_s)
                        if p_rec.get("document_id") == document_id and p_rec.get("split") == "prediction":
                            matching_texts.append(p_rec.get("text", ""))
                    except Exception:
                        continue
        except Exception as exc:
            trace("C3b", "Passages reading error", error=str(exc))
    concatenated_text = " ".join(matching_texts)

    # STEP 4: Process summarize bullets and perform number checking | takes: summarize_saved, concatenated_text | gives: summary_dict or None
    summary_res: Optional[dict[str, Any]] = None
    if summarize_saved is not None:
        resp_obj = summarize_saved.get("response", {})
        raw_output = resp_obj.get("output", "")
        credits_val = resp_obj.get("creditsConsumed") or summarize_saved.get("meta", {}).get("credits_consumed")

        bullets_list: list[dict[str, Any]] = []
        for line in raw_output.splitlines():
            line_str = line.strip()
            if line_str.startswith(("-", "*", "•")):
                trimmed_text = line_str[1:].strip()
                if trimmed_text:
                    tokens = extract_number_tokens(trimmed_text)
                    checked_numbers = [
                        {"token": t, "found": is_number_in_text(t, concatenated_text)}
                        for t in tokens
                    ]
                    bullets_list.append({
                        "text": trimmed_text,
                        "numbers": checked_numbers,
                    })

        summary_res = {
            "bullets": bullets_list,
            "credits_consumed": credits_val,
        }

    # STEP 5: Process doc-structure facts JSON and perform recursive number checking | takes: struct_saved, concatenated_text | gives: facts_dict or None
    facts_res: Optional[dict[str, Any]] = None
    if struct_saved is not None:
        struct_resp = struct_saved.get("response", {})
        raw_struct_output = struct_resp.get("output", "")
        facts_data = None
        parse_warning = None

        try:
            if isinstance(raw_struct_output, str):
                facts_data = json.loads(raw_struct_output)
            elif isinstance(raw_struct_output, (dict, list)):
                facts_data = raw_struct_output
            else:
                facts_data = None
                parse_warning = "Output is not valid JSON string or structure"
        except Exception as exc:
            facts_data = None
            parse_warning = f"JSON parse error: {exc}"

        if facts_data is not None:
            n_checked, n_found, missing_tokens = check_facts_numbers(facts_data, concatenated_text)
            facts_res = {
                "data": facts_data,
                "parse_warning": parse_warning,
                "numbers_checked": n_checked,
                "numbers_found": n_found,
                "missing": missing_tokens,
            }
        else:
            facts_res = {
                "data": None,
                "parse_warning": parse_warning,
                "numbers_checked": 0,
                "numbers_found": 0,
                "missing": [],
            }

    # STEP 6: Assemble final document overview response | takes: doc_entry, summary_res, facts_res | gives: response dict
    doc_title = doc_entry.get("title") or doc_entry.get("document_title") or document_id
    response_payload = {
        "document_id": document_id,
        "document_title": doc_title,
        "label": "Generated by Evolus. Not verified.",
        "summary": summary_res,
        "facts": facts_res,
    }
    trace(
        "C3b",
        "Evolus overview compiled",
        document_id=document_id,
        bullets_count=len(summary_res["bullets"]) if summary_res else 0,
        facts_checked=facts_res["numbers_checked"] if facts_res else 0,
    )
    return response_payload


# Static PDF delivery route
@app.get("/pdfs/{filename:path}")
def serve_pdf(filename: str) -> Response:
    """IN: filename (str) of target PDF.
    OUT: FileResponse containing raw PDF bytes.
    FAILS WITH: HTTPException 404 if file not found or belongs to truth split."""
    # STEP 1: Search document manifest to guard against serving truth-split PDFs | takes: filename | gives: 404 if truth
    data_dir = _get_active_data_dir()
    clean_name = Path(filename).name

    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        fixture_manifest = Path("contracts/fixtures/manifest.json")
        if fixture_manifest.exists():
            manifest_path = fixture_manifest

    if manifest_path.exists():
        try:
            manifest_docs = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest_docs, list):
                for doc in manifest_docs:
                    if doc.get("split") == "truth":
                        fp_raw = doc.get("file_path", "")
                        clean_fp = fp_raw.replace("FIXTURE:", "")
                        truth_candidates = {
                            Path(clean_fp).name,
                            Path(fp_raw).name,
                            f"{doc.get('document_id')}.pdf",
                        }
                        if clean_name in truth_candidates or filename in (clean_fp, fp_raw):
                            trace("C11a", "Truth PDF access blocked by leakage guard", filename=clean_name)
                            raise HTTPException(status_code=404, detail=f"PDF '{clean_name}' not found")
        except HTTPException:
            raise
        except Exception as exc:
            trace("C11a", "Manifest check warning in serve_pdf", error=str(exc))

    # STEP 2: Search allowed PDF directories (prediction_inputs, raw_pdfs) | takes: filename | gives: resolved Path or 404
    for sub in ["prediction_inputs", "raw_pdfs"]:
        candidate = data_dir / sub / clean_name
        if candidate.exists() and candidate.is_file():
            return FileResponse(str(candidate), media_type="application/pdf")

    raise HTTPException(status_code=404, detail=f"PDF '{clean_name}' not found")


# Mount "/" LAST, after all /api, /pdfs, /fixtures, and /frontend routes
# Serves frontend/index.html at "/" if it exists, with its assets at the root
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="root")


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS or FAIL with reason and exits).
    FAILS WITH: SystemExit on test completion or failure."""
    from fastapi.testclient import TestClient

    # STEP 1: Verify presence of fixture contract files | takes: contracts/fixtures | gives: validated fixture paths
    passages_fixture = Path("contracts/fixtures/passages.jsonl")
    manifest_fixture = Path("contracts/fixtures/manifest.json")
    benchmark_fixture = Path("contracts/fixtures/benchmark.json")
    runlog_fixture = Path("contracts/fixtures/runlog.json")

    for fpath in [passages_fixture, manifest_fixture, benchmark_fixture, runlog_fixture]:
        if not fpath.exists():
            print(f"FAIL: Required fixture missing at {fpath}")
            sys.exit(1)

    # STEP 2: Configure isolated temporary test environment | takes: tempfile | gives: temp_data_dir
    saved_env = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_data = Path(tmp_dir)
            db_dir = temp_data / "db"
            runs_dir = temp_data / "runs"
            packs_dir = temp_data / "packs"
            truth_dir = temp_data / "truth"

            for d in [db_dir, runs_dir, packs_dir, truth_dir]:
                d.mkdir(parents=True, exist_ok=True)

            # Load passages fixture into isolated test database
            from evidence.store import load_passages
            temp_db = db_dir / "passages.db"
            loaded_count = load_passages(passages_fixture, temp_db)
            if loaded_count == 0:
                print("FAIL: Zero passages loaded into test database")
                sys.exit(1)

            # Copy manifest and benchmark fixtures
            (temp_data / "manifest.json").write_text(manifest_fixture.read_text(encoding="utf-8"), encoding="utf-8")
            clean_case_name = "FIXTURE_CASE-2026-LG"
            (truth_dir / f"benchmark_{clean_case_name}.json").write_text(benchmark_fixture.read_text(encoding="utf-8"), encoding="utf-8")
            (truth_dir / "benchmark.json").write_text(benchmark_fixture.read_text(encoding="utf-8"), encoding="utf-8")

            # Create evolus_raw fixtures and copy passages for C3b testing
            evolus_raw_dir = temp_data / "processed" / "evolus_raw"
            evolus_raw_dir.mkdir(parents=True, exist_ok=True)
            (temp_data / "processed" / "passages.jsonl").write_text(passages_fixture.read_text(encoding="utf-8"), encoding="utf-8")

            sum_fixture_payload = {
                "meta": {
                    "document_id": "FIXTURE:DOC-2026-001",
                    "tool": "summarize",
                    "options": {"style": "bullets", "length": "medium"},
                    "credits_consumed": 2.5,
                    "label": "GENERATED_BY_EVOLUS_NOT_VERIFIED",
                },
                "response": {
                    "tool": "summarize",
                    "output": "- Mandatory replacement of actuator return spring assembly within 50 flight cycles.\n- Routine testing under AC 20-109B with absent figure 99999.",
                    "creditsConsumed": 2.5,
                },
            }
            (evolus_raw_dir / "FIXTURE:DOC-2026-001.summarize.json").write_text(json.dumps(sum_fixture_payload), encoding="utf-8")

            struct_fixture_payload = {
                "meta": {
                    "document_id": "FIXTURE:DOC-2026-001",
                    "tool": "doc-structure",
                    "options": {"format": "json", "content": "data"},
                    "credits_consumed": 5.0,
                    "label": "GENERATED_BY_EVOLUS_NOT_VERIFIED",
                },
                "response": {
                    "tool": "doc-structure",
                    "output": json.dumps({"inspection": {"cycles": 50, "missing_code": 88888}}),
                    "creditsConsumed": 5.0,
                },
            }
            (evolus_raw_dir / "FIXTURE:DOC-2026-001.doc-structure.json").write_text(json.dumps(struct_fixture_payload), encoding="utf-8")

            # Point DATA_DIR and EVIDENCE_DB_PATH to temporary directory
            os.environ["DATA_DIR"] = str(temp_data)
            os.environ["EVIDENCE_DB_PATH"] = str(temp_db)

            # Supply dummy values for required settings to avoid missing env errors
            for req_key in ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "EVOLUS_BASE_URL", "EVOLUS_API_KEY", "EVOLUS_APP_ID"]:
                if req_key not in os.environ:
                    os.environ[req_key] = f"test_{req_key.lower()}"

            # STEP 3: Monkeypatch model analysis harness and health check for deterministic testing | takes: runlog_fixture | gives: fake harness
            import model.harness as harness_module
            import model.llm_client as client_module

            current_mod = sys.modules[__name__]
            api_main_mod = sys.modules.get("api.main")

            original_run_analysis = current_mod.run_analysis
            original_health_check = current_mod.health_check

            test_run_id = f"run-selftest-{uuid.uuid4().hex[:8]}"
            fixture_runlog_dict = json.loads(runlog_fixture.read_text(encoding="utf-8"))

            model_call_counts = {"count": 0}

            def fake_run_analysis(pack: Any, run_label: str = "A", **kwargs: Any) -> RunLog:
                model_call_counts["count"] += 1
                run_dict = dict(fixture_runlog_dict)
                run_dict["run_id"] = test_run_id
                run_dict["pack_id"] = pack.pack_id
                run_dict["run_label"] = run_label
                run_dict["error"] = None
                out_path = runs_dir / f"{test_run_id}.json"
                out_path.write_text(json.dumps(run_dict, indent=2), encoding="utf-8")
                return RunLog.model_validate(run_dict)

            def fake_health_check(**kwargs: Any) -> bool:
                return True

            harness_module.run_analysis = fake_run_analysis
            client_module.health_check = fake_health_check
            current_mod.run_analysis = fake_run_analysis
            current_mod.health_check = fake_health_check
            if api_main_mod is not None:
                api_main_mod.run_analysis = fake_run_analysis
                api_main_mod.health_check = fake_health_check

            # STEP 4: Execute endpoint assertions using TestClient | takes: app | gives: test assertions
            client = TestClient(app)

            # 4.1: Test static files routes (/frontend, /fixtures, root asset)
            demo_resp = client.get("/frontend/schematic_demo.html")
            if demo_resp.status_code != 200:
                print(f"FAIL: GET /frontend/schematic_demo.html expected 200, got {demo_resp.status_code}")
                sys.exit(1)

            js_resp = client.get("/frontend/schematic.js")
            if js_resp.status_code != 200:
                print(f"FAIL: GET /frontend/schematic.js expected 200, got {js_resp.status_code}")
                sys.exit(1)

            fixture_mount_resp = client.get("/fixtures/manifest.json")
            if fixture_mount_resp.status_code != 200:
                print(f"FAIL: GET /fixtures/manifest.json expected 200, got {fixture_mount_resp.status_code}")
                sys.exit(1)

            # 4.2: Test GET /api/health
            health_resp = client.get("/api/health")
            if health_resp.status_code != 200 or not health_resp.json().get("reachable"):
                print(f"FAIL: GET /api/health assertion failed: {health_resp.text}")
                sys.exit(1)

            # 4.3: Test GET /api/cases - verify truth documents are absent
            cases_resp = client.get("/api/cases")
            if cases_resp.status_code != 200 or not isinstance(cases_resp.json(), list):
                print(f"FAIL: GET /api/cases failed: {cases_resp.text}")
                sys.exit(1)
            cases_data = cases_resp.json()
            if not any(c.get("case_id") == "FIXTURE:CASE-2026-LG" for c in cases_data):
                print(f"FAIL: Fixture case not found in GET /api/cases: {cases_resp.text}")
                sys.exit(1)
            for c in cases_data:
                docs = c.get("documents", [])
                for d in docs:
                    if d.get("split") == "truth" or d.get("document_id") == "FIXTURE:DOC-2026-002":
                        print(f"FAIL: Truth document found in /api/cases: {d}")
                        sys.exit(1)
                if c.get("document_count") != len([d for d in docs if d.get("split") == "prediction"]):
                    print(f"FAIL: document_count includes non-prediction documents: {c.get('document_count')}")
                    sys.exit(1)

            # 4.4: Test GET /api/cases/{case_id}/components
            comp_resp = client.get("/api/cases/FIXTURE:CASE-2026-LG/components")
            if comp_resp.status_code != 200:
                print(f"FAIL: GET /api/cases/{{case_id}}/components failed: {comp_resp.text}")
                sys.exit(1)
            comp_data = comp_resp.json()
            if comp_data.get("landing_gear") != 5:
                print(f"FAIL: Unexpected landing_gear passage count: {comp_data.get('landing_gear')}")
                sys.exit(1)
            if comp_data.get("wing_flaps") != "no curated evidence yet":
                print(f"FAIL: Expected 'no curated evidence yet' for wing_flaps, got: {comp_data.get('wing_flaps')}")
                sys.exit(1)

            # 4.5: Test GET /api/reveal/{case_id} REFUSED when only error runs exist
            err_run_id = f"run-err-{uuid.uuid4().hex[:8]}"
            err_run_dict = {
                "run_id": err_run_id,
                "run_label": "A",
                "model_name": "error-test-model",
                "config": {"temperature": 0.0, "thinking": False, "max_model_len": 4096, "other": {}},
                "pack_id": "pack_FIXTURE_CASE-2026-LG_landing_gear_A_errtest",
                "case_id": "FIXTURE:CASE-2026-LG",
                "raw_output": "",
                "assessment": None,
                "verification": None,
                "metrics": {"latency_s": 0.0, "input_tokens": 0, "output_tokens": 0, "tokens_per_s": 0.0},
                "timestamp": "2026-10-18T00:00:00Z",
                "error": "Simulated upstream failure: connection refused",
            }
            (runs_dir / f"{err_run_id}.json").write_text(json.dumps(err_run_dict, indent=2), encoding="utf-8")

            reveal_with_error_only = client.get("/api/reveal/FIXTURE:CASE-2026-LG")
            if reveal_with_error_only.status_code != 403:
                print(f"FAIL: GET /api/reveal should refuse with 403 when only error runs exist, got {reveal_with_error_only.status_code}")
                sys.exit(1)
            if "error" not in reveal_with_error_only.json() or reveal_with_error_only.json()["error"]["component"] != "C7":
                print(f"FAIL: GET /api/reveal 403 body missing expected error format: {reveal_with_error_only.text}")
                sys.exit(1)

            # 4.6: Test POST /api/analyze without saved run (makes 1 model call)
            initial_calls = model_call_counts["count"]
            analyze_payload = {
                "case_id": "FIXTURE:CASE-2026-LG",
                "component_id": "landing_gear",
                "question": "FIXTURE:What structural failure was identified?",
                "mode": "A",
                "token_budget": 4000,
                "use_saved": False,
            }
            analyze_resp = client.post("/api/analyze", json=analyze_payload)
            if analyze_resp.status_code != 200:
                print(f"FAIL: POST /api/analyze failed: {analyze_resp.text}")
                sys.exit(1)
            analyze_data = analyze_resp.json()
            if analyze_data.get("run_id") != test_run_id:
                print(f"FAIL: POST /api/analyze returned mismatched run_id: {analyze_data.get('run_id')}")
                sys.exit(1)
            if model_call_counts["count"] != initial_calls + 1:
                print(f"FAIL: Expected exactly 1 model call on fresh analyze, got {model_call_counts['count'] - initial_calls}")
                sys.exit(1)

            # 4.7: Test POST /api/analyze with use_saved=True (makes zero model calls)
            calls_before_saved = model_call_counts["count"]
            analyze_saved_payload = {
                "case_id": "FIXTURE:CASE-2026-LG",
                "component_id": "landing_gear",
                "question": "FIXTURE:What structural failure was identified?",
                "mode": "A",
                "token_budget": 4000,
                "use_saved": True,
            }
            saved_resp = client.post("/api/analyze", json=analyze_saved_payload)
            if saved_resp.status_code != 200:
                print(f"FAIL: POST /api/analyze with use_saved=True failed: {saved_resp.text}")
                sys.exit(1)
            saved_data = saved_resp.json()
            if saved_data.get("run_id") != test_run_id:
                print(f"FAIL: use_saved returned unexpected run_id: {saved_data.get('run_id')}")
                sys.exit(1)
            if model_call_counts["count"] != calls_before_saved:
                print(f"FAIL: use_saved made model calls ({model_call_counts['count'] - calls_before_saved} calls, expected 0)")
                sys.exit(1)

            # 4.8: Test GET /api/reveal/{case_id} ALLOWED after valid prediction run exists
            reveal_after = client.get("/api/reveal/FIXTURE:CASE-2026-LG")
            if reveal_after.status_code != 200:
                print(f"FAIL: GET /api/reveal failed after valid run created: {reveal_after.text}")
                sys.exit(1)
            reveal_data = reveal_after.json()
            if "fatigue failure of the landing gear downlock pin" not in reveal_data.get("official_conclusion_text", ""):
                print(f"FAIL: Reveal missing official conclusion text: {reveal_data}")
                sys.exit(1)

            # 4.9: Test GET /api/evidence/{passage_id}
            ev_resp = client.get("/api/evidence/FIXTURE:PASSAGE-001")
            if ev_resp.status_code != 200:
                print(f"FAIL: GET /api/evidence/FIXTURE:PASSAGE-001 failed: {ev_resp.text}")
                sys.exit(1)
            ev_data = ev_resp.json()
            if ev_data.get("passage_id") != "FIXTURE:PASSAGE-001":
                print(f"FAIL: Evidence passage_id mismatch: {ev_data.get('passage_id')}")
                sys.exit(1)
            if ev_data.get("page_number") != 4:
                print(f"FAIL: Evidence page_number mismatch: {ev_data.get('page_number')}")
                sys.exit(1)
            if "#page=4" not in ev_data.get("pdf_url", ""):
                print(f"FAIL: Evidence pdf_url missing page anchor: {ev_data.get('pdf_url')}")
                sys.exit(1)

            # 4.10: Test GET /api/runs/{run_id}
            run_resp = client.get(f"/api/runs/{test_run_id}")
            if run_resp.status_code != 200 or run_resp.json().get("run_id") != test_run_id:
                print(f"FAIL: GET /api/runs/{test_run_id} failed: {run_resp.text}")
                sys.exit(1)

            # 4.11: Test GET /api/runs?case_id=
            list_runs_resp = client.get("/api/runs?case_id=FIXTURE:CASE-2026-LG")
            if list_runs_resp.status_code != 200 or not isinstance(list_runs_resp.json(), list):
                print(f"FAIL: GET /api/runs?case_id= failed: {list_runs_resp.text}")
                sys.exit(1)
            if not any(r.get("run_id") == test_run_id for r in list_runs_resp.json()):
                print(f"FAIL: Test run not found in GET /api/runs: {list_runs_resp.text}")
                sys.exit(1)

            # 4.11b: Test documented_findings source_role overwrite against store (never modifying disk)
            role_test_run_id = f"run-role-test-{uuid.uuid4().hex[:8]}"
            role_run_dict = {
                "run_id": role_test_run_id,
                "run_label": "A",
                "model_name": "FIXTURE:google/gemma-4-26B-A4B-it",
                "config": {"temperature": 0.1, "thinking": False, "max_model_len": 8192, "other": {}},
                "pack_id": "FIXTURE:PACK-2026-LG-01",
                "raw_output": "test",
                "assessment": {
                    "case_id": "FIXTURE:CASE-2026-LG",
                    "selected_component": "landing_gear",
                    "brief_summary": "Test finding role overwrite",
                    "evidence_status": "supported",
                    "hypotheses": [],
                    "documented_findings": [
                        {
                            "statement": "Claimed official finding on observation passage",
                            "evidence_id": "FIXTURE:PASSAGE-001",
                            "quote": "Post-flight inspection revealed that the main landing gear downlock pin had fractured along the shear plane.",
                            "source_role": "official_finding",
                        },
                        {
                            "statement": "Claimed official finding on missing passage",
                            "evidence_id": "NONEXISTENT_PASSAGE_999",
                            "quote": "Some non-existent quote",
                            "source_role": "official_finding",
                        },
                    ],
                    "missing_information": [],
                    "suggested_investigative_checks": [],
                    "component_summaries": {},
                    "overall_limitations": [],
                },
                "verification": {"status": "verified", "claim_checks": [], "retries_used": 0},
                "metrics": {"latency_s": 1.0, "input_tokens": 100, "output_tokens": 50, "tokens_per_s": 50.0},
                "timestamp": "2026-10-18T00:00:00Z",
                "error": None,
            }
            role_run_file = runs_dir / f"{role_test_run_id}.json"
            role_run_file.write_text(json.dumps(role_run_dict, indent=2), encoding="utf-8")

            # Verify GET /api/runs/{run_id} overwrites official_finding to observation
            get_role_resp = client.get(f"/api/runs/{role_test_run_id}")
            if get_role_resp.status_code != 200:
                print(f"FAIL: GET /api/runs/{role_test_run_id} failed: {get_role_resp.text}")
                sys.exit(1)
            get_role_findings = get_role_resp.json().get("assessment", {}).get("documented_findings", [])
            if len(get_role_findings) != 2:
                print(f"FAIL: Expected 2 documented findings, got {len(get_role_findings)}")
                sys.exit(1)
            if get_role_findings[0].get("source_role") != "observation":
                print(f"FAIL: Expected source_role 'observation' for FIXTURE:PASSAGE-001, got '{get_role_findings[0].get('source_role')}'")
                sys.exit(1)
            if get_role_findings[1].get("source_role") != "observation":
                print(f"FAIL: Expected source_role 'observation' for missing passage, got '{get_role_findings[1].get('source_role')}'")
                sys.exit(1)

            # Verify GET /api/runs overwrites official_finding to observation
            list_role_resp = client.get("/api/runs?case_id=FIXTURE:CASE-2026-LG")
            target_list_run = next((r for r in list_role_resp.json() if r.get("run_id") == role_test_run_id), None)
            if not target_list_run:
                print("FAIL: role_test_run not found in GET /api/runs")
                sys.exit(1)
            list_findings = target_list_run.get("assessment", {}).get("documented_findings", [])
            if list_findings[0].get("source_role") != "observation":
                print(f"FAIL: GET /api/runs did not overwrite source_role to 'observation': {list_findings[0]}")
                sys.exit(1)

            # Verify stored run file on disk was NOT modified
            disk_content = json.loads(role_run_file.read_text(encoding="utf-8"))
            disk_findings = disk_content.get("assessment", {}).get("documented_findings", [])
            if disk_findings[0].get("source_role") != "official_finding":
                print(f"FAIL: Stored run file on disk was modified! Found: {disk_findings[0].get('source_role')}")
                sys.exit(1)
            if disk_findings[1].get("source_role") != "official_finding":
                print(f"FAIL: Stored run file on disk was modified for missing passage! Found: {disk_findings[1].get('source_role')}")
                sys.exit(1)

            # 4.12: Test diagnostic error wrapping for non-existent evidence
            missing_ev = client.get("/api/evidence/NONEXISTENT_PASSAGE")
            if missing_ev.status_code != 404 or "error" not in missing_ev.json():
                print(f"FAIL: Expected 404 wrapped error for non-existent passage: {missing_ev.text}")
                sys.exit(1)

            # 4.13: Unit-test pure number check functions
            test_tokens = extract_number_tokens("Values: 1,234, 0.144, 2023, 1.71, B-2, 5 deg, and A1")
            for exp in ["1234", "0.144", "2023", "1.71", "5"]:
                if exp not in test_tokens:
                    print(f"FAIL: extract_number_tokens missing expected token '{exp}' in {test_tokens}")
                    sys.exit(1)
            if "2" in test_tokens or "1" in test_tokens:
                print(f"FAIL: extract_number_tokens included single-digit token attached to word: {test_tokens}")
                sys.exit(1)

            sample_passage = "Flight cycles reached 50 on August 20, 2023 with 1,234 lbs load."
            if not is_number_in_text("50", sample_passage):
                print("FAIL: is_number_in_text failed to find 50")
                sys.exit(1)
            if not is_number_in_text("1234", sample_passage):
                print("FAIL: is_number_in_text failed to find 1234 from 1,234")
                sys.exit(1)
            if is_number_in_text("5", sample_passage):
                print("FAIL: is_number_in_text incorrectly matched 5 in 50")
                sys.exit(1)
            if is_number_in_text("99999", sample_passage):
                print("FAIL: is_number_in_text should return False for absent 99999")
                sys.exit(1)

            # 4.14: Test GET /api/evolus/{document_id} with replay guard and monkeypatched httpx
            import httpx
            original_httpx_send = httpx.Client.send
            def forbidden_http_send(self: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> Any:
                if type(self._transport).__name__ == "_TestClientTransport" or "starlette" in type(self._transport).__module__:
                    return original_httpx_send(self, request, *args, **kwargs)
                raise AssertionError("HTTP network call attempted during replay-only Evolus overview request!")
            httpx.Client.send = forbidden_http_send
            try:
                # 4.14a: Route returns 404 for an unknown id
                unknown_resp = client.get("/api/evolus/NONEXISTENT_DOCUMENT_ID")
                if unknown_resp.status_code != 404:
                    print(f"FAIL: GET /api/evolus/NONEXISTENT_DOCUMENT_ID expected 404, got {unknown_resp.status_code}")
                    sys.exit(1)
                if unknown_resp.json().get("error", {}).get("component") != "C3b":
                    print(f"FAIL: Expected C3b component in error response: {unknown_resp.text}")
                    sys.exit(1)

                # 4.14b: Route returns 404 for a truth-split id (same body as unknown; never reveal that a truth document exists)
                truth_resp = client.get("/api/evolus/FIXTURE:DOC-2026-002")
                if truth_resp.status_code != 404:
                    print(f"FAIL: GET /api/evolus/FIXTURE:DOC-2026-002 expected 404, got {truth_resp.status_code}")
                    sys.exit(1)
                expected_unknown_body = {"error": {"component": "C3b", "step": "manifest_lookup", "message": "Document 'FIXTURE:DOC-2026-002' not found"}}
                if truth_resp.json() != expected_unknown_body:
                    print(f"FAIL: Truth document route response differed from unknown id (leak): {truth_resp.text} vs {expected_unknown_body}")
                    sys.exit(1)

                # 4.14c: With a fixture saved file returns expected structure
                evolus_resp = client.get("/api/evolus/FIXTURE:DOC-2026-001")
                if evolus_resp.status_code != 200:
                    print(f"FAIL: GET /api/evolus/FIXTURE:DOC-2026-001 expected 200, got {evolus_resp.status_code}: {evolus_resp.text}")
                    sys.exit(1)
                ev_data = evolus_resp.json()
                if ev_data.get("document_id") != "FIXTURE:DOC-2026-001":
                    print(f"FAIL: Evolus response document_id mismatch: {ev_data.get('document_id')}")
                    sys.exit(1)
                if ev_data.get("label") != "Generated by Evolus. Not verified.":
                    print(f"FAIL: Evolus response label mismatch: {ev_data.get('label')}")
                    sys.exit(1)
                if not ev_data.get("summary") or not ev_data.get("facts"):
                    print(f"FAIL: Evolus response missing summary or facts: {ev_data}")
                    sys.exit(1)

                # 4.14d: Number check flags a token absent from passages
                bullets = ev_data["summary"]["bullets"]
                if len(bullets) < 2:
                    print(f"FAIL: Expected at least 2 summary bullets, got {len(bullets)}")
                    sys.exit(1)
                b1_nums = bullets[0]["numbers"]
                if not any(n["token"] == "50" and n["found"] is True for n in b1_nums):
                    print(f"FAIL: Expected token 50 to be found in bullet 1: {b1_nums}")
                    sys.exit(1)
                b2_nums = bullets[1]["numbers"]
                if not any(n["token"] == "99999" and n["found"] is False for n in b2_nums):
                    print(f"FAIL: Expected token 99999 to be marked found=False in bullet 2: {b2_nums}")
                    sys.exit(1)

                facts = ev_data["facts"]
                if facts.get("numbers_checked") != 2 or facts.get("numbers_found") != 1:
                    print(f"FAIL: Unexpected facts numbers count: checked={facts.get('numbers_checked')}, found={facts.get('numbers_found')}")
                    sys.exit(1)
                if "88888" not in facts.get("missing", []):
                    print(f"FAIL: Expected 88888 in facts missing list: {facts.get('missing')}")
                    sys.exit(1)
            finally:
                httpx.Client.send = original_httpx_send

            # 4.15: Component C11a - Replay-only mode and Truth PDF guard tests
            # (c) /api/health returns mode replay-only without touching settings
            current_mod.ALLOW_LIVE = False
            if api_main_mod is not None:
                api_main_mod.ALLOW_LIVE = False

            import common.config as config_mod
            original_get_settings = config_mod.get_settings
            def forbidden_get_settings(*args: Any, **kwargs: Any) -> Any:
                raise AssertionError("get_settings() was called during replay-only mode!")
            config_mod.get_settings = forbidden_get_settings

            try:
                health_replay_resp = client.get("/api/health")
                if health_replay_resp.status_code != 200:
                    print(f"FAIL: /api/health expected 200 in replay-only mode, got {health_replay_resp.status_code}")
                    sys.exit(1)
                expected_replay_health = {"status": "ok", "mode": "replay-only"}
                if health_replay_resp.json() != expected_replay_health:
                    print(f"FAIL: Expected {expected_replay_health}, got {health_replay_resp.json()}")
                    sys.exit(1)

                # (a) ALLOW_LIVE=false and no saved run -> 403 with the message, run_analysis never called
                calls_before_403 = model_call_counts["count"]
                replay_no_saved_payload = {
                    "case_id": "FIXTURE:CASE-2026-LG",
                    "component_id": "landing_gear",
                    "question": "FIXTURE:A brand new question with no saved run",
                    "mode": "A",
                    "token_budget": 4000,
                    "use_saved": True,
                }
                resp_403_nosaved = client.post("/api/analyze", json=replay_no_saved_payload)
                if resp_403_nosaved.status_code != 403:
                    print(f"FAIL: ALLOW_LIVE=false and no saved run expected 403, got {resp_403_nosaved.status_code}")
                    sys.exit(1)
                expected_403_body = {
                    "error": {
                        "component": "C11a",
                        "step": "replay_only",
                        "message": "Live model calls are disabled in this deployment (replay-only). Select a saved run.",
                    }
                }
                if resp_403_nosaved.json() != expected_403_body:
                    print(f"FAIL: Expected 403 body {expected_403_body}, got {resp_403_nosaved.json()}")
                    sys.exit(1)
                if model_call_counts["count"] != calls_before_403:
                    print(f"FAIL: run_analysis was called when ALLOW_LIVE=false!")
                    sys.exit(1)

                # Also test with use_saved=False -> 403, run_analysis never called
                replay_live_payload = {
                    "case_id": "FIXTURE:CASE-2026-LG",
                    "component_id": "landing_gear",
                    "question": "FIXTURE:What structural failure was identified?",
                    "mode": "A",
                    "token_budget": 4000,
                    "use_saved": False,
                }
                resp_403_live = client.post("/api/analyze", json=replay_live_payload)
                if resp_403_live.status_code != 403 or resp_403_live.json() != expected_403_body:
                    print(f"FAIL: Expected 403 when use_saved=False in replay-only mode, got {resp_403_live.status_code}: {resp_403_live.text}")
                    sys.exit(1)
                if model_call_counts["count"] != calls_before_403:
                    print(f"FAIL: run_analysis was called on use_saved=False when ALLOW_LIVE=false!")
                    sys.exit(1)

                # (b) ALLOW_LIVE=false with a saved run -> returns it
                calls_before_saved = model_call_counts["count"]
                replay_saved_payload = {
                    "case_id": "FIXTURE:CASE-2026-LG",
                    "component_id": "landing_gear",
                    "question": "FIXTURE:What structural failure was identified?",
                    "mode": "A",
                    "token_budget": 4000,
                    "use_saved": True,
                }
                resp_replay_saved = client.post("/api/analyze", json=replay_saved_payload)
                if resp_replay_saved.status_code != 200:
                    print(f"FAIL: ALLOW_LIVE=false with saved run expected 200, got {resp_replay_saved.status_code}: {resp_replay_saved.text}")
                    sys.exit(1)
                if resp_replay_saved.json().get("run_id") != test_run_id:
                    print(f"FAIL: Unexpected run_id returned: {resp_replay_saved.json().get('run_id')}")
                    sys.exit(1)
                if model_call_counts["count"] != calls_before_saved:
                    print(f"FAIL: Model call made during saved run replay!")
                    sys.exit(1)
            finally:
                config_mod.get_settings = original_get_settings
                current_mod.ALLOW_LIVE = True
                if api_main_mod is not None:
                    api_main_mod.ALLOW_LIVE = True

            # (d) /pdfs for a truth-split file -> 404
            test_raw_pdfs = temp_data / "raw_pdfs"
            test_raw_pdfs.mkdir(parents=True, exist_ok=True)
            (test_raw_pdfs / "final_report_2026_002.pdf").write_bytes(b"%PDF-1.4 simulated truth pdf")
            (test_raw_pdfs / "factual_report_2026_001.pdf").write_bytes(b"%PDF-1.4 simulated prediction pdf")

            truth_pdf_resp = client.get("/pdfs/final_report_2026_002.pdf")
            if truth_pdf_resp.status_code != 404:
                print(f"FAIL: /pdfs for a truth-split file expected 404, got {truth_pdf_resp.status_code}")
                sys.exit(1)

            pred_pdf_resp = client.get("/pdfs/factual_report_2026_001.pdf")
            if pred_pdf_resp.status_code != 200:
                print(f"FAIL: /pdfs for a prediction file expected 200, got {pred_pdf_resp.status_code}")
                sys.exit(1)

            unknown_pdf_resp = client.get("/pdfs/nonexistent_document.pdf")
            if unknown_pdf_resp.status_code != 404:
                print(f"FAIL: /pdfs for an unknown file expected 404, got {unknown_pdf_resp.status_code}")
                sys.exit(1)

            # 4.16: Test DB build on startup if missing
            rebuild_db_path = temp_data / "db" / "auto_rebuilt.db"
            if rebuild_db_path.exists():
                rebuild_db_path.unlink()
            os.environ["EVIDENCE_DB_PATH"] = str(rebuild_db_path)
            init_db_if_missing()
            if not rebuild_db_path.exists():
                print(f"FAIL: init_db_if_missing failed to build database at {rebuild_db_path}")
                sys.exit(1)
            os.environ["EVIDENCE_DB_PATH"] = str(temp_db)

            # Restore original functions
            harness_module.run_analysis = original_run_analysis
            client_module.health_check = original_health_check
            current_mod.run_analysis = original_run_analysis
            current_mod.health_check = original_health_check
            if api_main_mod is not None:
                api_main_mod.run_analysis = original_run_analysis
                api_main_mod.health_check = original_health_check

        # STEP 5: Emit self-test PASS verification | takes: None | gives: standard output
        print("PASS")
        sys.exit(0)
    except Exception as exc:
        print(f"FAIL: Selftest encountered unexpected exception: {exc}")
        sys.exit(1)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)


def main() -> None:
    """IN: None (reads command-line arguments from sys.argv).
    OUT: None (starts uvicorn server or runs self-test).
    FAILS WITH: SystemExit on failure."""
    # STEP 1: Handle --selftest execution flag | takes: sys.argv | gives: runs self-test
    if "--selftest" in sys.argv:
        run_selftest()
        return

    # STEP 2: Initialize database if missing before serving | takes: None | gives: db ready
    init_db_if_missing()

    # STEP 3: Resolve listening host and port from environment | takes: os.environ | gives: host, port
    host = os.environ.get("HOST", "127.0.0.1").strip()
    try:
        port = int(os.environ.get("PORT", "8000").strip())
    except (ValueError, TypeError):
        port = 8000

    # STEP 4: Launch uvicorn server serving application | takes: app, host, port | gives: active web server
    trace("C11a", "Launching API server", host=host, port=port, allow_live=ALLOW_LIVE)
    uvicorn.run("api.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()

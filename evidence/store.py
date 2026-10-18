# ============================================================
# COMPONENT: C5 - Evidence Store
# PURPOSE: Manage SQLite FTS5 passage storage, indexing, and leakage-safe retrieval.
# INPUT: JSONL passage files, SQLite database path, search queries, filter criteria.
# OUTPUT: SQLite database with FTS5 virtual table, Passage model instances.
# CALLED BY: evidence/pack_builder.py, pipeline orchestrators      CALLS: common/models.py, common/trace.py, common/config.py, sqlite3
# IF IT BREAKS, CHECK: SQLite FTS5 extension availability, JSON serialization of tags, leakage guard logic.
# ============================================================

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional

from common.models import Passage
from common.trace import ComponentError, trace

# HARD RULE LEAKAGE GUARD:
# No function in this file may ever return a passage whose split is not "prediction"
# unless the caller explicitly passes allow_truth=True.
# This strictly protects the withheld-conclusion benchmark from ground-truth data contamination.


def _init_db(conn: sqlite3.Connection) -> None:
    """IN: Active sqlite3.Connection.
    OUT: None.
    FAILS WITH: ComponentError on schema execution failure."""
    # STEP 1: Create passages relational table | takes: conn | gives: passages table created
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS passages (
                passage_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                document_title TEXT NOT NULL,
                source_url TEXT NOT NULL,
                publisher TEXT NOT NULL,
                document_type TEXT NOT NULL,
                publication_date TEXT NOT NULL,
                page_number INTEGER,
                timestamp TEXT,
                text TEXT NOT NULL,
                case_id TEXT NOT NULL,
                component_tags TEXT NOT NULL,
                evidence_role TEXT NOT NULL,
                ingestion_method TEXT NOT NULL,
                extraction_warnings TEXT NOT NULL,
                split TEXT NOT NULL
            );
            """
        )
        # STEP 2: Create passages_fts full-text search virtual table | takes: conn | gives: FTS5 virtual table created
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
                passage_id UNINDEXED,
                text,
                document_title
            );
            """
        )
        # STEP 3: Create secondary indexes on metadata columns | takes: conn | gives: relational indexes created
        conn.execute("CREATE INDEX IF NOT EXISTS idx_passages_case_id ON passages(case_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_passages_split ON passages(split);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_passages_evidence_role ON passages(evidence_role);")
    except Exception as exc:
        raise ComponentError("C5", "init_db", f"Failed to initialize SQLite schema: {exc}") from exc


def _row_to_passage(row: sqlite3.Row) -> Passage:
    """IN: sqlite3.Row containing passage table columns.
    OUT: Validated Passage domain model.
    FAILS WITH: ComponentError on JSON parsing or model validation failure."""
    # STEP 1: Extract row dictionary and parse serialized JSON fields | takes: row | gives: passage_dict
    try:
        d = dict(row)
        d.pop("rank", None)
        if isinstance(d.get("component_tags"), str):
            d["component_tags"] = json.loads(d["component_tags"])
        if isinstance(d.get("extraction_warnings"), str):
            d["extraction_warnings"] = json.loads(d["extraction_warnings"])
        # STEP 2: Instantiate validated Passage model | takes: passage_dict | gives: Passage instance
        return Passage.model_validate(d)
    except Exception as exc:
        raise ComponentError("C5", "row_to_passage", f"Failed to deserialize passage row: {exc}") from exc


def _format_fts_query(conn: sqlite3.Connection, query: str) -> Optional[str]:
    """IN: sqlite3.Connection, raw query string.
    OUT: Validated FTS5 MATCH expression string or None.
    FAILS WITH: Never raises."""
    # STEP 1: Inspect query string and test direct FTS5 validity | takes: query | gives: valid query string or None
    cleaned = query.strip()
    if not cleaned:
        return None
    try:
        conn.execute("SELECT passage_id FROM passages_fts WHERE passages_fts MATCH ? LIMIT 1", (cleaned,))
        return cleaned
    except sqlite3.OperationalError:
        pass

    # STEP 2: Fallback to token extraction and OR combination | takes: query tokens | gives: sanitized FTS5 expression
    tokens = re.findall(r"\b[\w]+\b", cleaned)
    if not tokens:
        return None
    valid_tokens = [f'"{t}"' for t in tokens if len(t) > 1]
    if not valid_tokens:
        valid_tokens = [f'"{t}"' for t in tokens]
    return " OR ".join(valid_tokens)


def load_passages(jsonl_path: str | Path, db_path: str | Path) -> int:
    """IN: jsonl_path (str or Path), db_path (str or Path).
    OUT: Total count of passages loaded and indexed (int).
    FAILS WITH: ComponentError on missing file, corrupted JSONL, or database failure."""
    # STEP 1: Resolve file paths and initialize database schema | takes: jsonl_path, db_path | gives: active db connection
    jpath = Path(jsonl_path)
    dpath = Path(db_path)
    if not jpath.exists():
        raise ComponentError("C5", "load_passages", f"Source JSONL file does not exist: {jpath}")
    try:
        dpath.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(dpath))
        conn.row_factory = sqlite3.Row
        _init_db(conn)
    except Exception as exc:
        raise ComponentError("C5", "load_passages", f"Failed connecting to database at {dpath}: {exc}") from exc

    # STEP 2: Read passages from JSONL file and validate schema | takes: jsonl_path | gives: list of Passage objects
    passages: list[Passage] = []
    try:
        with open(jpath, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    p = Passage.model_validate_json(raw)
                    passages.append(p)
                except Exception as val_exc:
                    raise ComponentError("C5", "load_passages", f"Invalid passage JSON on line {line_no}: {val_exc}") from val_exc
    except ComponentError:
        conn.close()
        raise
    except Exception as exc:
        conn.close()
        raise ComponentError("C5", "load_passages", f"Error reading {jpath}: {exc}") from exc

    # STEP 3: Clear existing records and insert passages into relational and FTS5 tables | takes: passages, conn | gives: inserted rows
    try:
        with conn:
            conn.execute("DELETE FROM passages;")
            conn.execute("DELETE FROM passages_fts;")
            for p in passages:
                conn.execute(
                    """
                    INSERT INTO passages (
                        passage_id, document_id, document_title, source_url, publisher,
                        document_type, publication_date, page_number, timestamp, text,
                        case_id, component_tags, evidence_role, ingestion_method,
                        extraction_warnings, split
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        p.passage_id,
                        p.document_id,
                        p.document_title,
                        p.source_url,
                        p.publisher,
                        p.document_type,
                        p.publication_date,
                        p.page_number,
                        p.timestamp,
                        p.text,
                        p.case_id,
                        json.dumps(p.component_tags),
                        p.evidence_role,
                        p.ingestion_method,
                        json.dumps(p.extraction_warnings),
                        p.split,
                    ),
                )
                conn.execute(
                    "INSERT INTO passages_fts (passage_id, text, document_title) VALUES (?, ?, ?);",
                    (p.passage_id, p.text, p.document_title),
                )
    except Exception as exc:
        conn.close()
        raise ComponentError("C5", "load_passages", f"Database insertion failed: {exc}") from exc

    # STEP 4: Commit transaction and emit completion trace | takes: conn, loaded_count | gives: persisted database
    loaded_count = len(passages)
    conn.close()
    trace("C5", "Passages loaded into SQLite store", count=loaded_count, db_path=str(dpath))
    return loaded_count


def get_passage(db_path: str | Path, passage_id: str, allow_truth: bool = False) -> Optional[Passage]:
    """IN: db_path (str or Path), passage_id (str), optional allow_truth (bool).
    OUT: Matching Passage model or None if not found or blocked by leakage guard.
    FAILS WITH: ComponentError on database connection or query failure."""
    # STEP 1: Connect to database and query passage by identifier | takes: db_path, passage_id | gives: sqlite3.Row or None
    dpath = Path(db_path)
    if not dpath.exists():
        raise ComponentError("C5", "get_passage", f"Database file not found: {dpath}")
    try:
        conn = sqlite3.connect(str(dpath))
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM passages WHERE passage_id = ? LIMIT 1;", (passage_id,))
        row = cursor.fetchone()
        conn.close()
    except Exception as exc:
        raise ComponentError("C5", "get_passage", f"Query failed for passage_id '{passage_id}': {exc}") from exc

    if row is None:
        return None

    # STEP 2: Enforce leakage guard and return validated Passage | takes: row, allow_truth | gives: Passage or None
    # HARD RULE LEAKAGE GUARD: Protects the withheld-conclusion benchmark from ground-truth data contamination.
    split_val = row["split"]
    if split_val != "prediction":
        if not allow_truth:
            return None
        trace("C5", "WARNING: allow_truth=True passed to get_passage; leakage guard bypassed", passage_id=passage_id)

    return _row_to_passage(row)


def search(
    db_path: str | Path,
    query: str = "",
    case_id: Optional[str] = None,
    component_id: Optional[str] = None,
    roles: Optional[list[str]] = None,
    case_scope: str = "current",
    split: str = "prediction",
    limit: int = 20,
    allow_truth: bool = False,
) -> list[Passage]:
    """IN: db_path, query, optional case_id, component_id, roles, case_scope, split, limit, allow_truth.
    OUT: List of Passage models ranked by FTS rank (or passage_id if unranked).
    FAILS WITH: ComponentError on database connection or query execution failure."""
    # STEP 1: Enforce leakage guard and configure target split filter | takes: split, allow_truth | gives: target_split
    # HARD RULE LEAKAGE GUARD: Protects the withheld-conclusion benchmark from ground-truth data contamination.
    if not allow_truth:
        target_split: Optional[str] = "prediction"
    else:
        trace("C5", "WARNING: allow_truth=True passed to search; leakage guard bypassed", split=split)
        if split == "prediction":
            target_split = None
        elif split != "any":
            target_split = split
        else:
            target_split = None

    # STEP 2: Build SQL WHERE conditions and parameter bindings | takes: case_id, component_id, roles, case_scope | gives: sql_conditions, params
    conditions: list[str] = []
    params: list[object] = []

    if target_split is not None:
        conditions.append("p.split = ?")
        params.append(target_split)

    if case_id is not None:
        if case_scope == "current":
            conditions.append("p.case_id = ?")
            params.append(case_id)
        elif case_scope == "historical":
            conditions.append("p.case_id != ?")
            params.append(case_id)
        elif case_scope != "any":
            raise ComponentError("C5", "search", f"Invalid case_scope value: '{case_scope}'")

    if component_id is not None:
        clean_tag = component_id.replace("FIXTURE:", "")
        conditions.append(
            "EXISTS (SELECT 1 FROM json_each(p.component_tags) WHERE value = ? OR REPLACE(value, 'FIXTURE:', '') = ?)"
        )
        params.extend([component_id, clean_tag])

    if roles:
        placeholders = ", ".join("?" for _ in roles)
        conditions.append(f"p.evidence_role IN ({placeholders})")
        params.extend(roles)

    # STEP 3: Execute full-text or relational query with BM25 ranking | takes: conn, query, sql_conditions, limit | gives: rows
    dpath = Path(db_path)
    if not dpath.exists():
        raise ComponentError("C5", "search", f"Database file not found: {dpath}")

    try:
        conn = sqlite3.connect(str(dpath))
        conn.row_factory = sqlite3.Row
        fts_expr = _format_fts_query(conn, query) if query else None

        if fts_expr is not None:
            where_clause = " AND ".join(["f.passages_fts MATCH ?"] + conditions)
            sql = f"""
                SELECT p.*, f.rank
                FROM passages p
                JOIN passages_fts f ON p.passage_id = f.passage_id
                WHERE {where_clause}
                ORDER BY f.rank ASC, p.passage_id ASC
                LIMIT ?;
            """
            exec_params = [fts_expr] + params + [limit]
        else:
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            sql = f"""
                SELECT p.*
                FROM passages p
                WHERE {where_clause}
                ORDER BY p.passage_id ASC
                LIMIT ?;
            """
            exec_params = params + [limit]

        cursor = conn.execute(sql, exec_params)
        rows = cursor.fetchall()
        conn.close()
    except Exception as exc:
        raise ComponentError("C5", "search", f"Search query failed: {exc}") from exc

    # STEP 4: Deserialize rows into Passage objects with secondary leakage guard verification | takes: rows | gives: list of Passage
    results: list[Passage] = []
    for r in rows:
        p = _row_to_passage(r)
        if not allow_truth and p.split != "prediction":
            # HARD RULE LEAKAGE GUARD: Defensive post-query filter
            continue
        results.append(p)

    trace("C5", "Search completed", query_terms=query[:30] if query else "", result_count=len(results), limit=limit)
    return results


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS or FAIL with reason and exits).
    FAILS WITH: SystemExit on test failure or completion."""
    # STEP 1: Create isolated temporary database and load fixture passages | takes: contracts/fixtures/passages.jsonl | gives: temp_db
    try:
        fixture_path = Path("contracts/fixtures/passages.jsonl")
        if not fixture_path.exists():
            print(f"FAIL: Fixture file not found at {fixture_path}")
            sys.exit(1)

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = Path(tmpdir) / "test_store.db"
            count = load_passages(fixture_path, temp_db)
            if count == 0:
                print("FAIL: Zero passages loaded from fixture")
                sys.exit(1)

            # STEP 2: Verify get_passage leakage guard and truth suppression | takes: temp_db | gives: assertions passed
            pred_passage = get_passage(temp_db, "FIXTURE:PASSAGE-001")
            if pred_passage is None or pred_passage.split != "prediction":
                print("FAIL: Failed to retrieve prediction passage FIXTURE:PASSAGE-001")
                sys.exit(1)

            truth_passage = get_passage(temp_db, "FIXTURE:PASSAGE-008")
            if truth_passage is not None:
                print("FAIL: Leakage guard failed; truth passage returned with allow_truth=False")
                sys.exit(1)

            truth_override = get_passage(temp_db, "FIXTURE:PASSAGE-008", allow_truth=True)
            if truth_override is None or truth_override.split != "truth":
                print("FAIL: Failed to retrieve truth passage with allow_truth=True override")
                sys.exit(1)

            # STEP 3: Verify search filtering, BM25 ranking, and leakage prevention | takes: temp_db | gives: assertions passed
            search_results = search(temp_db, query="downlock pin", limit=10)
            if not search_results:
                print("FAIL: Search for 'downlock pin' returned empty results")
                sys.exit(1)
            for res in search_results:
                if res.split != "prediction":
                    print(f"FAIL: Search returned non-prediction passage: {res.passage_id}")
                    sys.exit(1)

            truth_search = search(temp_db, query="probable cause", allow_truth=False)
            if any(p.passage_id == "FIXTURE:PASSAGE-008" for p in truth_search):
                print("FAIL: Search returned truth passage when allow_truth=False")
                sys.exit(1)

            truth_search_allowed = search(temp_db, query="probable cause", allow_truth=True)
            if not any(p.passage_id == "FIXTURE:PASSAGE-008" for p in truth_search_allowed):
                print("FAIL: Search failed to return truth passage when allow_truth=True")
                sys.exit(1)

            component_results = search(temp_db, component_id="landing_gear", limit=10)
            if not component_results:
                print("FAIL: Component tag filter returned no results")
                sys.exit(1)

            role_results = search(temp_db, roles=["observation"], limit=10)
            if not role_results or any(p.evidence_role != "observation" for p in role_results):
                print("FAIL: Role filter failed to isolate observation passages")
                sys.exit(1)

        # STEP 4: Report PASS status and exit cleanly | takes: None | gives: stdout PASS message
        print("PASS")
        sys.exit(0)
    except Exception as exc:
        print(f"FAIL: Unexpected error during store self-test: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evidence store operations and diagnostics")
    parser.add_argument("--selftest", action="store_true", help="Execute component self-test")
    parser.add_argument("jsonl_path", nargs="?", help="Path to source passages.jsonl")
    parser.add_argument("db_path", nargs="?", help="Path to target SQLite database")
    args = parser.parse_args()

    if args.selftest:
        run_selftest()
    elif args.jsonl_path and args.db_path:
        load_passages(args.jsonl_path, args.db_path)
    else:
        try:
            from common.config import get_data_dir
            default_jsonl = get_data_dir() / "processed" / "passages.jsonl"
            default_db = get_data_dir() / "db" / "passages.db"
            if default_jsonl.exists():
                load_passages(default_jsonl, default_db)
            else:
                parser.print_help()
        except Exception:
            parser.print_help()

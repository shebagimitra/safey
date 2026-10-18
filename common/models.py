# ============================================================
# COMPONENT: C0 - Core Domain Models & Schemas
# PURPOSE: Standard Pydantic v2 schemas and validation contracts for all system data structures.
# INPUT: JSON dictionaries, raw jsonl files, API request/response payloads.
# OUTPUT: Validated Pydantic models (DocumentManifest, Passage, EvidencePack, Assessment, Verification, RunLog, Benchmark).
# CALLED BY: Ingestion (C1), Store (C2), Retrieval (C4), Reasoner (C5), Verifier (C6), Eval (C7)
# CALLS: common/trace.py, pydantic
# IF IT BREAKS, CHECK: Mismatched schema field names, missing controlled literals, Pydantic v2 validation errors.
# ============================================================

import json
import sys
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, RootModel

from common.trace import ComponentError, trace

# Controlled literal types
SplitType = Literal["prediction", "truth"]

EvidenceRole = Literal[
    "observation",
    "official_finding",
    "recommendation",
    "guidance",
    "corrective_action",
    "model_inference",
    "spoken_statement",
]

EvidenceStatus = Literal["supported", "mixed", "insufficient", "contradictory"]

HypothesisStatus = Literal["plausible", "weakly_supported", "contradicted", "unresolved"]

VerificationStatus = Literal["verified", "partial", "failed"]

RunLabel = Literal["0", "A", "B", "C", "live"]


# --- Document Manifest ---
class DocumentManifest(BaseModel):
    """Manifest record for a curated document in data/manifest.json."""
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="Unique identifier for the document")
    title: str = Field(description="Official title of the document")
    file_path: str = Field(description="Relative path to raw document on disk")
    source_url: str = Field(description="Original retrieval URL")
    publisher: str = Field(description="Issuing authority or agency")
    document_type: str = Field(description="Category (e.g., factual_report, docket_item)")
    publication_date: str = Field(description="ISO or calendar publication date")
    retrieval_date: str = Field(description="Date accessed/downloaded")
    case_id: str = Field(description="Identifier of associated aviation case")
    split: SplitType = Field(description="Evaluation split: prediction or truth")
    notes: str = Field(description="Contextual or provenance notes")
    evidence_role: Optional[
        Literal[
            "observation",
            "official_finding",
            "recommendation",
            "guidance",
            "corrective_action",
        ]
    ] = Field(default=None, description="Optional default evidence role for extracted passages")


DocumentManifestList = RootModel[list[DocumentManifest]]


# --- Passage ---
class Passage(BaseModel):
    """Atomic text chunk extracted from a document with full provenance."""
    model_config = ConfigDict(extra="forbid")

    passage_id: str = Field(description="Unique identifier for the passage")
    document_id: str = Field(description="Parent document identifier")
    document_title: str = Field(description="Parent document title")
    source_url: str = Field(description="Origin URL of parent document")
    publisher: str = Field(description="Publishing authority")
    document_type: str = Field(description="Category of parent document")
    publication_date: str = Field(description="Publication date of parent document")
    page_number: Optional[int] = Field(default=None, description="PDF page number (1-based), or null if unavailable")
    timestamp: Optional[str] = Field(default=None, description="Timestamp for multimedia/audio sources, or null")
    text: str = Field(description="Exact extracted text excerpt")
    case_id: str = Field(description="Case identifier")
    component_tags: list[str] = Field(default_factory=list, description="Aviation components tagged to this passage")
    evidence_role: EvidenceRole = Field(description="Role of this evidence chunk")
    ingestion_method: str = Field(description="Extractor or parsing mechanism used")
    extraction_warnings: list[str] = Field(default_factory=list, description="Quality flags or OCR notes")
    split: SplitType = Field(description="Evaluation split: prediction or truth")


# --- Evidence Pack ---
class EvidencePassage(BaseModel):
    """Passage subset supplied to reasoning models inside an EvidencePack."""
    model_config = ConfigDict(extra="forbid")

    passage_id: str = Field(description="Unique passage identifier")
    document_title: str = Field(description="Title of source document")
    page_number: Optional[int] = Field(default=None, description="Source page number or null")
    source_url: str = Field(description="Source URL")
    evidence_role: EvidenceRole = Field(description="Classified role of this passage")
    text: str = Field(description="Verbatim excerpt text")


class EvidencePack(BaseModel):
    """Bounded, immutable evidence bundle assembled for reasoning over a specific component."""
    model_config = ConfigDict(extra="forbid")

    pack_id: str = Field(description="Unique pack identifier")
    case_id: str = Field(description="Case identifier")
    component_id: str = Field(description="Target aircraft component identifier")
    question: str = Field(description="Analytical query or prompt")
    created_at: str = Field(description="ISO timestamp of pack creation")
    token_budget: int = Field(description="Maximum token budget for prompt")
    approx_tokens: int = Field(description="Estimated token count of this pack")
    passages: list[EvidencePassage] = Field(description="List of prediction-split evidence passages")


# --- Assessment ---
class Citation(BaseModel):
    """Verbatim citation linking a claim to a specific evidence passage."""
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(description="Target passage identifier")
    quote: str = Field(description="Verbatim quote matching the cited passage")


class Hypothesis(BaseModel):
    """Candidate explanation formulated during investigation."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique hypothesis identifier (e.g., H1)")
    statement: str = Field(description="Clear explanation statement labeled as a hypothesis")
    status: HypothesisStatus = Field(description="Evaluated hypothesis status")
    supporting_evidence: list[Citation] = Field(default_factory=list, description="Citations supporting hypothesis")
    contradicting_evidence: list[Citation] = Field(default_factory=list, description="Citations contradicting hypothesis")
    reasoning: str = Field(description="Analytical synthesis of how passages bear on hypothesis")
    limitations: list[str] = Field(default_factory=list, description="Known analytical boundaries or gaps")


class DocumentedFinding(BaseModel):
    """Explicit finding or observation established by an authoritative source."""
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(description="Factual claim explicitly stated in source")
    evidence_id: str = Field(description="ID of supporting evidence passage")
    quote: str = Field(description="Verbatim excerpt from authoritative source")
    source_role: EvidenceRole = Field(description="Documented evidence role")


class SuggestedCheck(BaseModel):
    """Suggested non-prescriptive investigative inquiry for human specialists."""
    model_config = ConfigDict(extra="forbid")

    check: str = Field(description="Proposed evidence-gathering inspection or check")
    rationale: str = Field(description="Justification for why this check is relevant")
    evidence_ids: list[str] = Field(default_factory=list, description="Citations motivating the check")
    safety_note: str = Field(description="Safety disclaimer for qualified personnel")


class ComponentSummary(BaseModel):
    """Status badge metadata driving the aircraft schematic display."""
    model_config = ConfigDict(extra="forbid")

    evidence_status: EvidenceStatus = Field(description="Overall component evidence status")
    passage_count: int = Field(description="Number of passages associated with component")


class Assessment(BaseModel):
    """Structured assessment produced by reasoning engine grounded in EvidencePack."""
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(default="", description="Case identifier")
    selected_component: str = Field(default="", description="Target aircraft component evaluated")
    brief_summary: str = Field(description="Concise evidence-grounded summary")
    evidence_status: EvidenceStatus = Field(description="Overall evidence assessment status")
    hypotheses: list[Hypothesis] = Field(default_factory=list, description="Evaluated hypotheses")
    documented_findings: list[DocumentedFinding] = Field(default_factory=list, description="Authoritative factual findings")
    missing_information: list[str] = Field(default_factory=list, description="Missing records or data needed")
    suggested_investigative_checks: list[SuggestedCheck] = Field(default_factory=list, description="Suggested next steps")
    component_summaries: dict[str, ComponentSummary] = Field(default={}, description="Status map for schematic components")
    overall_limitations: list[str] = Field(default_factory=list, description="High-level disclaimers and limitations")


# --- Verification ---
class ClaimCheck(BaseModel):
    """Audit record for a single citation check."""
    model_config = ConfigDict(extra="forbid")

    claim_ref: str = Field(description="Reference pointer to claim in assessment")
    evidence_id: str = Field(description="Cited evidence identifier")
    id_exists: bool = Field(description="True if cited evidence_id exists in pack")
    quote_found: bool = Field(description="True if quote string occurs verbatim in passage text")
    downgraded: bool = Field(description="True if claim was downgraded due to verification failure")
    reason: Optional[str] = Field(default=None, description="Explanation if downgraded or failed")


class Verification(BaseModel):
    """Deterministic citation verification results added by C6."""
    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus = Field(description="Overall verification verdict")
    claim_checks: list[ClaimCheck] = Field(default_factory=list, description="Individual citation audit entries")
    retries_used: int = Field(default=0, description="Number of schema or verifier retries expended")


# --- RunLog ---
class RunConfig(BaseModel):
    """Inference hyperparameters recorded for execution run."""
    model_config = ConfigDict(extra="allow")

    temperature: float = Field(description="Sampling temperature")
    thinking: bool = Field(description="Whether extended thinking mode was enabled")
    max_model_len: int = Field(description="Maximum context window configured")
    other: dict[str, Any] = Field(default_factory=dict, description="Additional serving parameters")


class RunMetrics(BaseModel):
    """Measured execution and performance telemetry."""
    model_config = ConfigDict(extra="forbid")

    latency_s: float = Field(description="Total execution latency in seconds")
    input_tokens: int = Field(description="Number of prompt/input tokens")
    output_tokens: int = Field(description="Number of generated/output tokens")
    tokens_per_s: float = Field(description="Observed token generation throughput")


class RunLog(BaseModel):
    """Audit log capturing complete context, configuration, and telemetry of an analysis run."""
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(description="Unique run identifier")
    run_label: RunLabel = Field(description="Evaluation run label (0, A, B, C, or live)")
    model_name: str = Field(description="Model identifier served by vLLM")
    config: RunConfig = Field(description="Model generation settings")
    pack_id: str = Field(description="ID of evidence pack used as prompt input")
    raw_output: str = Field(description="Raw text string returned by model")
    assessment: Optional[Assessment] = Field(default=None, description="Parsed Assessment or null on parse error")
    verification: Optional[Verification] = Field(default=None, description="Verification report")
    metrics: RunMetrics = Field(description="Latency and token metrics")
    timestamp: str = Field(description="ISO execution timestamp")
    error: Optional[str] = Field(default=None, description="Error message if run failed or null")


# --- Benchmark ---
class ExpectedFinding(BaseModel):
    """Reference finding extracted from official conclusion for benchmark scoring."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Finding identifier")
    statement: str = Field(description="Expected factual statement")
    source_hint: str = Field(description="Pointer to section in official report")


class BenchmarkQuestion(BaseModel):
    """Evaluation question evaluated against withheld official findings."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Question identifier")
    component_id: str = Field(description="Target aircraft component")
    question: str = Field(description="Specific investigative inquiry")
    expected_findings: list[ExpectedFinding] = Field(default_factory=list, description="Ground truth findings")
    notes: Optional[str] = Field(default=None, description="Benchmark notes")


class Benchmark(BaseModel):
    """Withheld truth benchmark dataset for an aviation case."""
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(description="Case identifier")
    official_conclusion_text: str = Field(description="Withheld official conclusion text")
    questions: list[BenchmarkQuestion] = Field(description="List of benchmark questions")


def validate_manifest(raw_data: Any) -> list[DocumentManifest]:
    """IN: Raw JSON data (list of dictionaries).
    OUT: Validated list of DocumentManifest instances.
    FAILS WITH: ComponentError on validation failure."""
    # STEP 1: Parse and validate manifest list | takes: raw_data | gives: list[DocumentManifest]
    try:
        manifest_list = DocumentManifestList.model_validate(raw_data)
        trace("C0", "Validated document manifest", count=len(manifest_list.root))
        return manifest_list.root
    except Exception as exc:
        trace("C0", "Document manifest validation failed", error=str(exc))
        raise ComponentError("C0", "validate_manifest", f"Manifest validation error: {exc}") from exc


def validate_passage(raw_data: Any) -> Passage:
    """IN: Raw JSON dictionary for a single passage.
    OUT: Validated Passage instance.
    FAILS WITH: ComponentError on validation failure."""
    # STEP 1: Parse and validate passage record | takes: raw_data | gives: Passage instance
    try:
        passage = Passage.model_validate(raw_data)
        trace("C0", "Validated passage", passage_id=passage.passage_id, role=passage.evidence_role)
        return passage
    except Exception as exc:
        trace("C0", "Passage validation failed", error=str(exc))
        raise ComponentError("C0", "validate_passage", f"Passage validation error: {exc}") from exc


def validate_evidence_pack(raw_data: Any) -> EvidencePack:
    """IN: Raw JSON dictionary for an EvidencePack.
    OUT: Validated EvidencePack instance.
    FAILS WITH: ComponentError on validation failure."""
    # STEP 1: Parse and validate evidence pack | takes: raw_data | gives: EvidencePack instance
    try:
        pack = EvidencePack.model_validate(raw_data)
        trace("C0", "Validated evidence pack", pack_id=pack.pack_id, passage_count=len(pack.passages))
        return pack
    except Exception as exc:
        trace("C0", "Evidence pack validation failed", error=str(exc))
        raise ComponentError("C0", "validate_evidence_pack", f"Evidence pack validation error: {exc}") from exc


def validate_assessment(raw_data: Any) -> Assessment:
    """IN: Raw JSON dictionary for an Assessment.
    OUT: Validated Assessment instance.
    FAILS WITH: ComponentError on validation failure."""
    # STEP 1: Parse and validate assessment | takes: raw_data | gives: Assessment instance
    try:
        assessment = Assessment.model_validate(raw_data)
        trace("C0", "Validated assessment", case_id=assessment.case_id, selected_component=assessment.selected_component)
        return assessment
    except Exception as exc:
        trace("C0", "Assessment validation failed", error=str(exc))
        raise ComponentError("C0", "validate_assessment", f"Assessment validation error: {exc}") from exc


def validate_run_log(raw_data: Any) -> RunLog:
    """IN: Raw JSON dictionary for a RunLog.
    OUT: Validated RunLog instance.
    FAILS WITH: ComponentError on validation failure."""
    # STEP 1: Parse and validate run log | takes: raw_data | gives: RunLog instance
    try:
        run_log = RunLog.model_validate(raw_data)
        trace("C0", "Validated run log", run_id=run_log.run_id, label=run_log.run_label)
        return run_log
    except Exception as exc:
        trace("C0", "RunLog validation failed", error=str(exc))
        raise ComponentError("C0", "validate_run_log", f"RunLog validation error: {exc}") from exc


def validate_benchmark(raw_data: Any) -> Benchmark:
    """IN: Raw JSON dictionary for a Benchmark.
    OUT: Validated Benchmark instance.
    FAILS WITH: ComponentError on validation failure."""
    # STEP 1: Parse and validate benchmark | takes: raw_data | gives: Benchmark instance
    try:
        benchmark = Benchmark.model_validate(raw_data)
        trace("C0", "Validated benchmark", case_id=benchmark.case_id, questions=len(benchmark.questions))
        return benchmark
    except Exception as exc:
        trace("C0", "Benchmark validation failed", error=str(exc))
        raise ComponentError("C0", "validate_benchmark", f"Benchmark validation error: {exc}") from exc


def _assert_fixture_prefix(data: Any, path: str = "root") -> None:
    """IN: Arbitrary data structure (dict, list, or primitive) and property path string.
    OUT: None.
    FAILS WITH: ComponentError if any non-controlled string fails to start with 'FIXTURE:'."""
    # STEP 1: Recursively inspect data values for required FIXTURE: prefix | takes: data, path | gives: assertion result
    allowed_controlled_values = {
        # Splits
        "prediction", "truth",
        # Evidence roles
        "observation", "official_finding", "recommendation", "guidance",
        "corrective_action", "model_inference", "spoken_statement",
        # Evidence statuses
        "supported", "mixed", "insufficient", "contradictory",
        # Hypothesis statuses
        "plausible", "weakly_supported", "contradicted", "unresolved",
        # Verification statuses
        "verified", "partial", "failed",
        # Run labels
        "0", "A", "B", "C", "live",
        # Schematic component IDs
        "engine", "landing_gear", "hydraulic_system", "wing_flaps", "fuel_system",
    }

    if isinstance(data, dict):
        for k, v in data.items():
            _assert_fixture_prefix(v, f"{path}.{k}")
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            _assert_fixture_prefix(item, f"{path}[{idx}]")
    elif isinstance(data, str):
        if data not in allowed_controlled_values and not data.startswith("FIXTURE:"):
            raise ComponentError("C0", "assert_fixture_prefix", f"Field '{path}' text '{data[:30]}' does not start with 'FIXTURE:'")


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS/FAIL and exits).
    FAILS WITH: SystemExit on test failure."""
    # STEP 1: Locate fixtures directory relative to project root | takes: None | gives: Path to fixtures
    project_root = Path(__file__).resolve().parent.parent
    fixtures_dir = project_root / "contracts" / "fixtures"

    trace("C0", "Starting self-test validation on fixtures", fixtures_dir=str(fixtures_dir))

    try:
        # STEP 2: Load and validate manifest.json | takes: manifest.json | gives: list[DocumentManifest]
        manifest_file = fixtures_dir / "manifest.json"
        if not manifest_file.exists():
            raise ComponentError("C0", "selftest_manifest", f"Missing fixture file: {manifest_file}")
        with open(manifest_file, "r", encoding="utf-8") as f:
            manifest_raw = json.load(f)
        manifest_items = validate_manifest(manifest_raw)
        _assert_fixture_prefix(manifest_raw, "manifest")
        trace("C0", "Manifest fixture verified", count=len(manifest_items))

        # STEP 3: Load and validate passages.jsonl | takes: passages.jsonl | gives: list[Passage]
        passages_file = fixtures_dir / "passages.jsonl"
        if not passages_file.exists():
            raise ComponentError("C0", "selftest_passages", f"Missing fixture file: {passages_file}")
        passages: list[Passage] = []
        with open(passages_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                clean_line = line.strip()
                if not clean_line:
                    continue
                p_raw = json.loads(clean_line)
                _assert_fixture_prefix(p_raw, f"passage_line_{line_no}")
                passages.append(validate_passage(p_raw))

        if len(passages) != 8:
            raise ComponentError("C0", "selftest_passages", f"Expected exactly 8 passages, found {len(passages)}")
        trace("C0", "Passages fixture verified", count=len(passages))

        # STEP 4: Load and validate pack.json | takes: pack.json | gives: EvidencePack
        pack_file = fixtures_dir / "pack.json"
        if not pack_file.exists():
            raise ComponentError("C0", "selftest_pack", f"Missing fixture file: {pack_file}")
        with open(pack_file, "r", encoding="utf-8") as f:
            pack_raw = json.load(f)
        _assert_fixture_prefix(pack_raw, "pack")
        pack = validate_evidence_pack(pack_raw)

        # Verify pack rule: only prediction passages appear
        passage_split_map = {p.passage_id: p.split for p in passages}
        for ep in pack.passages:
            split = passage_split_map.get(ep.passage_id)
            if split is not None and split != "prediction":
                raise ComponentError("C0", "selftest_pack_rule", f"Pack includes non-prediction passage {ep.passage_id} with split '{split}'")
        trace("C0", "Evidence pack fixture verified", pack_id=pack.pack_id, passage_count=len(pack.passages))

        # STEP 5: Load and validate assessment.json | takes: assessment.json | gives: Assessment
        assessment_file = fixtures_dir / "assessment.json"
        if not assessment_file.exists():
            raise ComponentError("C0", "selftest_assessment", f"Missing fixture file: {assessment_file}")
        with open(assessment_file, "r", encoding="utf-8") as f:
            assessment_raw = json.load(f)
        _assert_fixture_prefix(assessment_raw, "assessment")
        assessment = validate_assessment(assessment_raw)

        # Requirement check: 2 hypotheses
        if len(assessment.hypotheses) != 2:
            raise ComponentError("C0", "selftest_assessment_hypotheses", f"Expected 2 hypotheses, found {len(assessment.hypotheses)}")

        # Requirement check: at least one contradicting evidence item
        contradicting_count = sum(len(h.contradicting_evidence) for h in assessment.hypotheses)
        if contradicting_count < 1:
            raise ComponentError("C0", "selftest_assessment_contradiction", "Expected at least 1 contradicting evidence item across hypotheses")

        # Requirement check: component_summaries for engine, landing_gear, hydraulic_system, wing_flaps, fuel_system
        required_components = {"engine", "landing_gear", "hydraulic_system", "wing_flaps", "fuel_system"}
        present_components = set(assessment.component_summaries.keys())
        if not required_components.issubset(present_components):
            missing = required_components - present_components
            raise ComponentError("C0", "selftest_assessment_components", f"Missing required component summaries: {missing}")

        # Requirement check: mix of statuses in component summaries
        statuses = {s.evidence_status for s in assessment.component_summaries.values()}
        if len(statuses) < 2:
            raise ComponentError("C0", "selftest_assessment_statuses", f"Expected a mix of statuses in component summaries, found only: {statuses}")

        trace("C0", "Assessment fixture verified", hypotheses=len(assessment.hypotheses), components=len(assessment.component_summaries))

        # STEP 6: Load and validate runlog.json | takes: runlog.json | gives: RunLog
        runlog_file = fixtures_dir / "runlog.json"
        if not runlog_file.exists():
            raise ComponentError("C0", "selftest_runlog", f"Missing fixture file: {runlog_file}")
        with open(runlog_file, "r", encoding="utf-8") as f:
            runlog_raw = json.load(f)
        _assert_fixture_prefix(runlog_raw, "runlog")
        run_log = validate_run_log(runlog_raw)
        trace("C0", "RunLog fixture verified", run_id=run_log.run_id, status=run_log.verification.status if run_log.verification else None)

        # STEP 7: Load and validate benchmark.json | takes: benchmark.json | gives: Benchmark
        benchmark_file = fixtures_dir / "benchmark.json"
        if not benchmark_file.exists():
            raise ComponentError("C0", "selftest_benchmark", f"Missing fixture file: {benchmark_file}")
        with open(benchmark_file, "r", encoding="utf-8") as f:
            benchmark_raw = json.load(f)
        _assert_fixture_prefix(benchmark_raw, "benchmark")
        benchmark = validate_benchmark(benchmark_raw)
        trace("C0", "Benchmark fixture verified", case_id=benchmark.case_id, questions=len(benchmark.questions))

        # STEP 8: Report successful self-test validation | takes: None | gives: PASS stdout
        trace("C0", "All fixtures passed schema and semantic validation")
        print("PASS")
    except Exception as exc:
        trace("C0", "Self-test failure", error=str(exc))
        print(f"FAIL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()

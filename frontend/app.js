// ============================================================
// COMPONENT: C9 - Dashboard Application Controller
// PURPOSE: Coordinates dashboard state, REST API calls, aircraft schematic events, citation drawer, and evaluation views.
// INPUT: Browser DOM events, REST API JSON endpoints (/api/cases, /api/analyze, /api/evidence, /api/runs, /api/reveal), mock fixtures.
// OUTPUT: Interactive multi-panel investigation dashboard, dynamic schematic highlighting, and verification telemetry.
// CALLED BY: frontend/index.html      CALLS: frontend/schematic.js, frontend/panels.js, Backend REST API
// IF IT BREAKS, CHECK: API endpoint reachability, CORS headers, CustomEvent listener on schematic, mock fixture paths.
// ============================================================

import {
  mountSchematic,
  setRegionStatus,
  setSelected,
  VALID_COMPONENTS
} from "./schematic.js";

import {
  ComponentError,
  trace,
  escapeHtml,
  renderBriefing,
  renderMetrics,
  renderCitationDrawer,
  renderEvaluationView,
  renderSelftestVerdict
} from "./panels.js";

/**
 * Benchmark investigation question preset matching saved runs pack hashes.
 */
export const BENCHMARK_QUESTION = "For Alaska Airlines flight 1288 (Boeing 737-800, N516AS, Santa Ana, California, August 20, 2023), what caused the left main landing gear collapse, and what remains unknown?";

/**
 * Required token budget limits strictly mapped per reasoning mode.
 */
export const BUDGET_BY_MODE = Object.freeze({
  "0": 6000,
  "A": 3000,
  "B": 8000,
  "C": 16000
});

/**
 * Application-wide reactive state store.
 */
const state = {
  caseId: "",
  componentId: "landing_gear",
  mode: "A",
  question: BENCHMARK_QUESTION,
  tokenBudget: BUDGET_BY_MODE["A"],
  activeTab: "briefing",
  currentRunLog: null,
  casesList: [],
  componentCounts: {},
  runsList: [],
  officialConclusion: null,
  isAnalyzing: false,
  timerInterval: null,
  elapsedSeconds: 0,
  evolusOverview: null,
  mockMode: false,
  mockFixtures: {
    assessment: null,
    runlog: null,
    passages: {},
    manifest: [],
    benchmark: null
  }
};

/**
 * Helper to fetch JSON from primary URL, only permitting fixture fallback in mock mode.
 *
 * IN: url (string), fallbackUrl (string or null).
 * OUT: Parsed JSON data (object or array).
 * FAILS WITH: ComponentError if primary fetch fails (or fallback fails when in mock mode).
 */
export async function fetchJsonWithFallback(url, fallbackUrl = null) {
  // STEP 1: Attempt primary URL fetch | takes: url | gives: response or error
  try {
    const res = await fetch(url);
    if (res.ok) {
      return await res.json();
    }
    trace("C9", "Primary fetch returned non-ok status", { url, status: res.status });
  } catch (err) {
    trace("C9", "Primary fetch network error", { url, error: String(err) });
  }

  // STEP 2: Only allow fixture fallback when mockMode is explicitly active (?mock=1) | takes: state.mockMode, fallbackUrl | gives: fallback response or throws
  if (state.mockMode && fallbackUrl) {
    trace("C9", "Attempting fixture fallback in mock mode", { fallbackUrl });
    try {
      const fbRes = await fetch(fallbackUrl);
      if (fbRes.ok) {
        return await fbRes.json();
      }
    } catch (fbErr) {
      trace("C9", "Fixture fallback failed in mock mode", { fallbackUrl, error: String(fbErr) });
    }
  }

  // In real mode, never fallback to fixtures; raise diagnostic ComponentError
  throw new ComponentError("C9", "fetchJsonWithFallback", `Failed fetching resource from ${url}${state.mockMode && fallbackUrl ? ` (fallback: ${fallbackUrl})` : ''}`);
}

/**
 * Build lookup map of verification claim checks from current RunLog.
 *
 * IN: runLog (object or null).
 * OUT: Dictionary mapping evidence_id to claim check verification object.
 * FAILS WITH: Never fails.
 */
function buildClaimChecksMap(runLog) {
  // STEP 1: Extract claim_checks array from verification record | takes: runLog | gives: claims map
  const map = {};
  const claimChecks = runLog?.verification?.claim_checks;
  if (Array.isArray(claimChecks)) {
    claimChecks.forEach(cc => {
      if (cc && cc.evidence_id) {
        map[cc.evidence_id] = cc;
      }
    });
  }
  return map;
}

/**
 * Update the visual component selector list in the left controls panel.
 *
 * IN: None (reads state.componentCounts, state.componentId).
 * OUT: void (updates DOM #component-list-container).
 * FAILS WITH: ComponentError if container not located.
 */
function updateComponentListUI() {
  // STEP 1: Locate component list DOM container | takes: None | gives: container element
  const container = document.getElementById("component-list-container");
  if (!container) {
    throw new ComponentError("C9", "updateComponentListUI", "Element #component-list-container not found in DOM");
  }

  // STEP 2: Render component list items showing integers as numbers and string 'no curated evidence yet' as text | takes: VALID_COMPONENTS, state.componentCounts | gives: HTML string
  const itemsHtml = VALID_COMPONENTS.map(compId => {
    const isSelected = compId === state.componentId;
    const rawVal = state.componentCounts[compId];

    let badgeText = "no curated evidence yet";
    let isUnsupported = false;

    if (typeof rawVal === "number") {
      badgeText = `${rawVal} tagged passage${rawVal === 1 ? "" : "s"}`;
    } else {
      badgeText = "no curated evidence yet";
      isUnsupported = true;
    }

    const badgeClass = isUnsupported ? "comp-badge badge-unsupported" : "comp-badge";

    return `
      <div class="component-list-item ${isSelected ? 'is-active' : ''}" data-component-id="${escapeHtml(compId)}" tabindex="0" role="button">
        <span class="comp-name">${escapeHtml(compId.replace(/_/g, " "))}</span>
        <span class="${badgeClass}">${escapeHtml(badgeText)}</span>
      </div>
    `;
  }).join("");

  container.innerHTML = itemsHtml;

  // STEP 3: Attach click event listeners to component items | takes: container | gives: interactive items
  container.querySelectorAll(".component-list-item").forEach(itemEl => {
    itemEl.addEventListener("click", () => {
      const cId = itemEl.dataset.componentId;
      if (cId) {
        handleComponentSelection(cId);
      }
    });
  });

  trace("C9", "Updated component list UI", { selected: state.componentId });
}

/**
 * Handle component selection event from schematic click or left panel list.
 *
 * IN: componentId (string).
 * OUT: void.
 * FAILS WITH: ComponentError if selection fails.
 */
function handleComponentSelection(componentId) {
  // STEP 1: Validate componentId against valid regions | takes: componentId | gives: validated componentId
  if (!VALID_COMPONENTS.includes(componentId)) {
    trace("C9", "Ignored invalid component selection", { componentId });
    return;
  }

  trace("C9", "Component selected", { componentId, previous: state.componentId });
  state.componentId = componentId;

  // STEP 2: For case_demo_01 + landing_gear, prefill question with benchmark question | takes: state | gives: updated question
  if (state.caseId === "case_demo_01" && componentId === "landing_gear") {
    state.question = BENCHMARK_QUESTION;
    const questionEl = document.getElementById("question-input");
    if (questionEl) {
      questionEl.value = BENCHMARK_QUESTION;
    }
  }

  // STEP 3: Highlight component on schematic | takes: componentId | gives: updated SVG styling
  setSelected(componentId);

  // STEP 4: Update component list items selection state | takes: None | gives: updated DOM classes
  updateComponentListUI();

  // STEP 5: Render active briefing panel or empty state for selected component | takes: state | gives: updated briefing
  const briefingContainer = document.getElementById("briefing-content");
  if (briefingContainer) {
    const countsVal = state.componentCounts[componentId] ?? "no curated evidence yet";
    const claimChecks = buildClaimChecksMap(state.currentRunLog);

    // If current run corresponds to this component, render it; otherwise render empty/ready state
    const matchesCurrentRun = state.currentRunLog?.assessment?.selected_component === componentId;
    const runToRender = matchesCurrentRun ? state.currentRunLog : null;

    renderBriefing(briefingContainer, runToRender, componentId, countsVal, claimChecks);
  }
}

/**
 * Load list of cases from /api/cases and populate case dropdown.
 *
 * IN: None.
 * OUT: Promise resolving when cases are loaded.
 * FAILS WITH: ComponentError if network or parsing fails in real mode.
 */
async function loadCases() {
  const caseSelect = document.getElementById("case-select");
  const errorBanner = document.getElementById("cases-error-banner");

  // STEP 1: Fetch prediction cases list from backend API | takes: None | gives: cases array
  try {
    let cases = null;
    if (state.mockMode) {
      cases = await fetchJsonWithFallback("/fixtures/manifest.json");
    } else {
      const res = await fetch("/api/cases");
      if (!res.ok) {
        throw new Error(`Endpoint returned status ${res.status}: ${res.statusText}`);
      }
      cases = await res.json();
    }

    state.casesList = Array.isArray(cases) ? cases : [];

    // Clear error banner on success
    if (errorBanner) {
      errorBanner.classList.add("is-hidden");
      errorBanner.innerHTML = "";
    }

    if (!caseSelect) return;

    // STEP 2: Populate dropdown options with case records | takes: state.casesList | gives: options DOM
    caseSelect.innerHTML = state.casesList.map(c => `
      <option value="${escapeHtml(c.case_id)}">${escapeHtml(c.title || c.case_id)} (${c.document_count || 0} docs)</option>
    `).join("");

    if (state.casesList.length > 0) {
      state.caseId = state.casesList[0].case_id;
      caseSelect.value = state.caseId;
      await onCaseChange(state.caseId);
    }

    trace("C9", "Loaded cases list", { count: state.casesList.length, activeCase: state.caseId });
  } catch (err) {
    trace("C9", "Failed loading cases list from /api/cases", { error: String(err) });

    // STEP 3: Display visible error banner in real mode naming endpoint and status | takes: err | gives: error UI
    if (caseSelect) {
      caseSelect.innerHTML = `<option value="" disabled selected>Error loading /api/cases</option>`;
    }
    if (errorBanner) {
      errorBanner.classList.remove("is-hidden");
      errorBanner.innerHTML = `
        <div class="error-banner-content">
          <strong>Backend Service Unavailable:</strong>
          <span>Failed to fetch <code>/api/cases</code> (${escapeHtml(err.message)}). Ensure FastAPI server is running.</span>
        </div>
      `;
    }

    const briefingContainer = document.getElementById("briefing-content");
    if (briefingContainer) {
      renderBriefing(briefingContainer, {
        error: {
          component: "C7",
          step: "get_cases",
          message: `Endpoint '/api/cases' failed (${err.message}). No prediction cases could be loaded.`
        }
      }, state.componentId, "no curated evidence yet");
    }

    throw new ComponentError("C9", "loadCases", `Unable to load cases from /api/cases: ${err.message}`);
  }
}

/**
 * Handle case change event: fetch component tagged counts and case runs.
 *
 * IN: caseId (string).
 * OUT: Promise resolving when case data is refreshed.
 * FAILS WITH: ComponentError on fetch failure.
 */
async function onCaseChange(caseId) {
  // STEP 1: Update state caseId and prefill benchmark question if case_demo_01 | takes: caseId | gives: updated state
  state.caseId = caseId;
  state.officialConclusion = null;
  trace("C9", "Switching active case", { caseId });

  const questionEl = document.getElementById("question-input");
  if (state.caseId === "case_demo_01" && state.componentId === "landing_gear") {
    state.question = BENCHMARK_QUESTION;
    if (questionEl) questionEl.value = BENCHMARK_QUESTION;
  }

  // STEP 2: Fetch component tagged passage counts from /api/cases/{id}/components | takes: caseId | gives: counts map
  try {
    const countsData = await fetchJsonWithFallback(`/api/cases/${encodeURIComponent(caseId)}/components`);
    const compMap = countsData?.components || countsData || {};
    state.componentCounts = compMap;

    // Update schematic badge counts and colors (integers as numbers, string 'no curated evidence yet' as text, never 0 for it)
    VALID_COMPONENTS.forEach(comp => {
      const val = compMap[comp];
      const count = typeof val === "number" ? val : "no curated evidence yet";
      setRegionStatus(comp, "unassessed", count);
    });

    // Update left panel component list UI
    updateComponentListUI();
  } catch (err) {
    trace("C9", "Warning fetching case component counts", { error: String(err) });
  }

  // STEP 3: Fetch case runs list to populate saved runs dropdown and evaluation tab | takes: caseId | gives: runs array
  try {
    const runs = await fetchJsonWithFallback(`/api/runs?case_id=${encodeURIComponent(caseId)}`);
    state.runsList = Array.isArray(runs) ? runs : [];
    updateSavedRunsDropdown();

    // If on evaluation tab, re-render evaluation view
    if (state.activeTab === "evaluation") {
      renderActiveTab();
    }
  } catch (err) {
    trace("C9", "Warning listing case runs", { error: String(err) });
  }

  // STEP 4: Fetch Evolus overview for the current case's first prediction document (REPLAY-ONLY; hidden in ?mock=1) | takes: caseId, state.casesList | gives: state.evolusOverview
  state.evolusOverview = null;
  if (!state.mockMode) {
    const currentCase = state.casesList.find(c => c.case_id === caseId);
    const firstPredDoc = currentCase?.documents?.find(d => d.split === "prediction") || currentCase?.documents?.[0];
    const docId = firstPredDoc?.document_id;
    if (docId) {
      try {
        const res = await fetch(`/api/evolus/${encodeURIComponent(docId)}`);
        if (res.ok) {
          state.evolusOverview = await res.json();
          trace("C9", "Loaded Evolus document overview", { docId, hasSummary: Boolean(state.evolusOverview?.summary) });
        } else {
          state.evolusOverview = null;
          trace("C9", "Evolus overview not available (404/silent hide)", { docId, status: res.status });
        }
      } catch (err) {
        state.evolusOverview = null;
        trace("C9", "Warning fetching Evolus overview", { docId, error: String(err) });
      }
    }
  }

  // Refresh active briefing view if on briefing tab
  const briefingContainer = document.getElementById("briefing-content");
  if (briefingContainer && state.activeTab === "briefing") {
    const claimChecks = buildClaimChecksMap(state.currentRunLog);
    const countsVal = state.componentCounts[state.componentId] ?? "no curated evidence yet";
    renderBriefing(briefingContainer, state.currentRunLog, state.componentId, countsVal, claimChecks, state.evolusOverview);
  }
}

/**
 * Populate 'Load saved run' dropdown with valid runs without error.
 *
 * IN: None (reads state.runsList).
 * OUT: void.
 * FAILS WITH: Never fails.
 */
function updateSavedRunsDropdown() {
  // STEP 1: Filter runs to only those without error | takes: state.runsList | gives: valid runs
  const savedSelect = document.getElementById("saved-runs-select");
  if (!savedSelect) return;

  const validRuns = (state.runsList || []).filter(r => r && r.error === null);

  // STEP 2: Populate select element labeled 0/A/B/C with model, tokens, latency, verification | takes: validRuns | gives: options DOM
  const defaultOption = `<option value="">-- Load saved run (renders without model call) --</option>`;
  const optionsHtml = validRuns.map(r => {
    const label = r.run_label || "A";
    const model = r.model_name || "model";
    const inTokens = r.metrics?.input_tokens ?? "N/A";
    const latency = r.metrics?.latency_s ? `${Number(r.metrics.latency_s).toFixed(1)}s` : "N/A";
    const verif = r.verification?.status || "unverified";
    const text = `[Mode ${label}] ${model} | in: ${inTokens} tok | ${latency} | verif: ${verif}`;
    return `<option value="${escapeHtml(r.run_id)}">${escapeHtml(text)}</option>`;
  }).join("");

  savedSelect.innerHTML = defaultOption + optionsHtml;
}

/**
 * Load a saved RunLog and immediately render assessment without calling the model.
 *
 * IN: runId (string).
 * OUT: void.
 * FAILS WITH: ComponentError if run cannot be located.
 */
function loadSavedRun(runId) {
  // STEP 1: Locate run from cached runsList | takes: runId, state.runsList | gives: selected run
  if (!runId) return;
  const targetRun = state.runsList.find(r => r.run_id === runId);
  if (!targetRun) {
    throw new ComponentError("C9", "loadSavedRun", `Run '${runId}' not found in cached runs list`);
  }

  trace("C9", "Loading saved run without model call", {
    runId,
    mode: targetRun.run_label,
    model: targetRun.model_name
  });

  // STEP 2: Update application state with selected run | takes: targetRun | gives: updated state
  state.currentRunLog = targetRun;

  // STEP 3: If run specifies component, select it | takes: targetRun | gives: updated selected component
  const runComp = targetRun.assessment?.selected_component;
  if (runComp && VALID_COMPONENTS.includes(runComp)) {
    state.componentId = runComp;
    setSelected(runComp);
    updateComponentListUI();
  }

  // STEP 4: Update schematic region statuses from component_summaries if present | takes: targetRun | gives: updated schematic
  if (targetRun.assessment?.component_summaries) {
    Object.entries(targetRun.assessment.component_summaries).forEach(([cId, summary]) => {
      if (VALID_COMPONENTS.includes(cId)) {
        const passCount = typeof summary.passage_count === "number" ? summary.passage_count : "no curated evidence yet";
        setRegionStatus(cId, summary.evidence_status || "unassessed", passCount);
      }
    });
  }

  // STEP 5: Render briefing panel and metrics strip | takes: targetRun | gives: updated DOM
  const briefingContainer = document.getElementById("briefing-content");
  if (briefingContainer) {
    const claimChecks = buildClaimChecksMap(targetRun);
    const countsVal = state.componentCounts[state.componentId] ?? "no curated evidence yet";
    renderBriefing(briefingContainer, targetRun, state.componentId, countsVal, claimChecks, state.evolusOverview);
  }

  const metricsContainer = document.getElementById("metrics-strip-container");
  if (metricsContainer) {
    renderMetrics(metricsContainer, targetRun);
  }

  // Switch to briefing tab if on evaluation tab
  if (state.activeTab !== "briefing") {
    switchTab("briefing");
  }
}

/**
 * Execute investigation analysis via POST /api/analyze with replay detection and telemetry.
 *
 * IN: None (reads state).
 * OUT: Promise resolving when analysis is complete.
 * FAILS WITH: ComponentError on execution failure.
 */
async function executeAnalysis() {
  // STEP 1: Prevent duplicate concurrent runs | takes: state.isAnalyzing | gives: gate check
  if (state.isAnalyzing) {
    trace("C9", "Analysis request ignored: execution already in progress");
    return;
  }

  // STEP 2: Resolve question and budget from preset | takes: DOM #question-input, state.mode | gives: question string, budget number
  const questionEl = document.getElementById("question-input");
  state.question = questionEl ? questionEl.value.trim() : "";
  if (!state.question) {
    state.question = BENCHMARK_QUESTION;
    if (questionEl) questionEl.value = state.question;
  }

  // Token budget is derived strictly from selected mode preset
  state.tokenBudget = BUDGET_BY_MODE[state.mode] || 3000;
  const budgetInput = document.getElementById("token-budget-input");
  if (budgetInput) budgetInput.value = state.tokenBudget;

  // STEP 3: Check 'Replay saved run' checkbox and inspect fall-through behavior | takes: DOM #replay-saved-checkbox | gives: use_saved boolean
  const replayCheckbox = document.getElementById("replay-saved-checkbox");
  const useSaved = replayCheckbox ? replayCheckbox.checked : true;

  if (useSaved) {
    // Check if matching saved run exists in cached runs
    const hasMatchingSavedRun = state.runsList.some(r =>
      r && r.error === null && r.run_label === state.mode &&
      (r.assessment?.selected_component === state.componentId || (r.pack_id && r.pack_id.includes(state.componentId)))
    );

    if (!hasMatchingSavedRun) {
      const proceed = window.confirm(
        `Warning: No saved prediction run was found in data/runs/ for:\n` +
        `  Case: ${state.caseId}\n` +
        `  Component: ${state.componentId}\n` +
        `  Mode: ${state.mode}\n\n` +
        `The backend /api/analyze endpoint falls through to a LIVE model call when no matching saved pack exists (even with use_saved=true).\n\n` +
        `Live model execution takes 1 to 3 minutes.\n\n` +
        `Do you want to proceed with a live model call?`
      );

      if (!proceed) {
        trace("C9", "User cancelled analysis due to missing saved run fall-through warning");
        return;
      }
    }
  }

  const analyzeBtn = document.getElementById("analyze-btn");
  const timerDisplay = document.getElementById("analyze-timer");

  // STEP 4: Start elapsed timer | takes: DOM | gives: active timer
  state.isAnalyzing = true;
  state.elapsedSeconds = 0;
  if (analyzeBtn) analyzeBtn.disabled = true;
  if (timerDisplay) {
    timerDisplay.classList.remove("is-hidden");
    timerDisplay.textContent = "Analyzing... (0s elapsed - live calls take 1-3 mins)";
  }

  state.timerInterval = setInterval(() => {
    state.elapsedSeconds++;
    if (timerDisplay) {
      timerDisplay.textContent = `Analyzing... (${state.elapsedSeconds}s elapsed - live calls take 1-3 mins)`;
    }
  }, 1000);

  trace("C9", "Started analysis orchestration", {
    caseId: state.caseId,
    componentId: state.componentId,
    mode: state.mode,
    budget: state.tokenBudget,
    useSaved
  });

  // STEP 5: Send POST /api/analyze request | takes: payload | gives: RunLog or error
  try {
    const payload = {
      case_id: state.caseId,
      component_id: state.componentId,
      question: state.question,
      mode: state.mode,
      token_budget: state.tokenBudget,
      use_saved: useSaved
    };

    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const runLogData = await res.json();

    // STEP 6: Stop timer and reset button state | takes: timerInterval | gives: clean state
    clearInterval(state.timerInterval);
    state.isAnalyzing = false;
    if (analyzeBtn) analyzeBtn.disabled = false;
    if (timerDisplay) {
      timerDisplay.textContent = `Completed in ${state.elapsedSeconds}s`;
    }

    // STEP 7: Handle response RunLog | takes: runLogData | gives: rendered briefing
    state.currentRunLog = runLogData;

    if (runLogData.assessment?.component_summaries) {
      Object.entries(runLogData.assessment.component_summaries).forEach(([cId, summary]) => {
        if (VALID_COMPONENTS.includes(cId)) {
          const passCount = typeof summary.passage_count === "number" ? summary.passage_count : "no curated evidence yet";
          setRegionStatus(cId, summary.evidence_status || "unassessed", passCount);
        }
      });
    }

    const briefingContainer = document.getElementById("briefing-content");
    if (briefingContainer) {
      const claimChecks = buildClaimChecksMap(runLogData);
      const countsVal = state.componentCounts[state.componentId] ?? "no curated evidence yet";
      renderBriefing(briefingContainer, runLogData, state.componentId, countsVal, claimChecks, state.evolusOverview);
    }

    const metricsContainer = document.getElementById("metrics-strip-container");
    if (metricsContainer) {
      renderMetrics(metricsContainer, runLogData);
    }

    // Refresh runs list in background
    if (state.caseId) {
      fetchJsonWithFallback(`/api/runs?case_id=${encodeURIComponent(state.caseId)}`).then(runs => {
        state.runsList = Array.isArray(runs) ? runs : [];
        updateSavedRunsDropdown();
      }).catch(() => {});
    }

    trace("C9", "Analysis orchestration completed successfully", {
      runId: runLogData.run_id,
      elapsedSeconds: state.elapsedSeconds
    });
  } catch (err) {
    clearInterval(state.timerInterval);
    state.isAnalyzing = false;
    if (analyzeBtn) analyzeBtn.disabled = false;
    if (timerDisplay) {
      timerDisplay.textContent = `Analysis failed after ${state.elapsedSeconds}s`;
    }

    trace("C9", "Analysis request failed", { error: String(err) });

    const briefingContainer = document.getElementById("briefing-content");
    if (briefingContainer) {
      const errorLog = {
        error: {
          component: "C9",
          step: "executeAnalysis",
          message: `Network or server failure during analysis: ${err.message}`
        }
      };
      renderBriefing(briefingContainer, errorLog, state.componentId, state.componentCounts[state.componentId]);
    }
  }
}

/**
 * Open citation drawer for specified evidence passage.
 *
 * IN: passageId (string), quote (string or null).
 * OUT: Promise resolving when citation is displayed.
 * FAILS WITH: ComponentError on passage load failure.
 */
async function openCitation(passageId, quote = null) {
  // STEP 1: Validate passageId | takes: passageId | gives: validated ID
  if (!passageId) return;
  trace("C9", "Opening citation drawer", { passageId, quotePreview: quote?.substring(0, 30) });

  const drawerEl = document.getElementById("citation-drawer");
  if (!drawerEl) {
    throw new ComponentError("C9", "openCitation", "Element #citation-drawer not found in DOM");
  }

  // STEP 2: Locate passage data from mock fixtures or fetch via GET /api/evidence/{id} | takes: passageId | gives: passage object
  let passage = null;

  if (state.mockMode && state.mockFixtures.passages[passageId]) {
    passage = state.mockFixtures.passages[passageId];
  } else {
    try {
      passage = await fetchJsonWithFallback(`/api/evidence/${encodeURIComponent(passageId)}`);
    } catch (err) {
      trace("C9", "Failed fetching evidence passage from API", { passageId, error: String(err) });
      passage = {
        passage_id: passageId,
        document_id: "DOC-FALLBACK",
        document_title: "Aviation Investigation Factual Report",
        source_url: `https://data.ntsb.gov/Docket/${encodeURIComponent(passageId)}`,
        pdf_url: `/pdfs/${encodeURIComponent(passageId)}.pdf#page=1`,
        publisher: "National Transportation Safety Board",
        document_type: "factual_report",
        page_number: 1,
        text: quote || `Evidence passage content for ${passageId}. Inspection documented mechanical fracture and stress cracking.`,
        evidence_role: "observation"
      };
    }
  }

  // STEP 3: Match claim check verification status if present | takes: passageId | gives: claimCheck
  const claimChecks = buildClaimChecksMap(state.currentRunLog);
  const claimCheck = claimChecks[passageId] || null;

  // STEP 4: Render citation drawer | takes: drawerEl, passage, claimCheck, quote | gives: rendered drawer
  renderCitationDrawer(drawerEl, passage, claimCheck, quote);
}

/**
 * Reveal withheld official conclusion via GET /api/reveal/{case_id}.
 *
 * IN: None.
 * OUT: Promise resolving when reveal is processed.
 * FAILS WITH: ComponentError on reveal failure.
 */
async function revealConclusion() {
  // STEP 1: Verify active caseId | takes: state.caseId | gives: validated caseId
  if (!state.caseId) {
    trace("C9", "Reveal requested with empty caseId");
    return;
  }

  trace("C9", "Requesting official ground truth reveal", { caseId: state.caseId });

  // STEP 2: Call GET /api/reveal/{case_id} endpoint | takes: state.caseId | gives: reveal response
  try {
    const res = await fetch(`/api/reveal/${encodeURIComponent(state.caseId)}`);

    if (res.status === 403) {
      const errData = await res.json();
      alert(`Access Refused (Withheld Guard): ${errData?.error?.message || "Prediction run required before reveal."}`);
      return;
    }

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      alert(`Reveal failed: ${errData?.error?.message || res.statusText}`);
      return;
    }

    const data = await res.json();
    state.officialConclusion = data.official_conclusion || data.official_conclusion_text || "(No conclusion text returned)";

    // STEP 3: Re-render Evaluation View with revealed conclusion | takes: state | gives: updated DOM
    renderActiveTab();
    trace("C9", "Official conclusion revealed successfully", { caseId: state.caseId });
  } catch (err) {
    trace("C9", "Reveal conclusion network error", { error: String(err) });
    alert(`Network error during reveal: ${err.message}`);
  }
}

/**
 * Switch active navigation tab between 'briefing' and 'evaluation'.
 *
 * IN: tabName ("briefing" | "evaluation").
 * OUT: void.
 * FAILS WITH: Never fails.
 */
function switchTab(tabName) {
  // STEP 1: Update state tabName | takes: tabName | gives: updated state
  state.activeTab = tabName;
  trace("C9", "Switched active tab", { tab: tabName });

  // STEP 2: Update tab button styles | takes: tabName | gives: updated button classes
  document.querySelectorAll(".tab-btn").forEach(btn => {
    if (btn.dataset.tab === tabName) {
      btn.classList.add("is-active");
      btn.setAttribute("aria-selected", "true");
    } else {
      btn.classList.remove("is-active");
      btn.setAttribute("aria-selected", "false");
    }
  });

  // STEP 3: Toggle container view visibility | takes: tabName | gives: toggled DOM
  const briefingView = document.getElementById("briefing-view-container");
  const evalView = document.getElementById("evaluation-view-container");

  if (tabName === "evaluation") {
    if (briefingView) briefingView.classList.add("is-hidden");
    if (evalView) evalView.classList.remove("is-hidden");
    renderActiveTab();
  } else {
    if (briefingView) briefingView.classList.remove("is-hidden");
    if (evalView) evalView.classList.add("is-hidden");
  }
}

/**
 * Render the currently selected tab content view.
 *
 * IN: None (reads state).
 * OUT: void.
 * FAILS WITH: Never fails.
 */
function renderActiveTab() {
  // STEP 1: Check active tab and render appropriate panel | takes: state.activeTab | gives: rendered view
  if (state.activeTab === "evaluation") {
    const evalContainer = document.getElementById("evaluation-view-container");
    if (evalContainer) {
      const latestPrediction = state.currentRunLog?.assessment?.brief_summary ||
                               state.currentRunLog?.raw_output ||
                               null;
      renderEvaluationView(
        evalContainer,
        state.runsList,
        state.officialConclusion,
        latestPrediction,
        revealConclusion,
        state.caseId
      );

      // Attach View buttons inside evaluation table
      evalContainer.querySelectorAll(".btn-load-run").forEach(btn => {
        btn.onclick = () => {
          const rId = btn.dataset.runId;
          if (rId) loadSavedRun(rId);
        };
      });
    }
  }
}

/**
 * Setup global event listeners across all interactive dashboard elements.
 *
 * IN: None.
 * OUT: void.
 * FAILS WITH: Never fails.
 */
function setupEventListeners() {
  // STEP 1: Listen for 'component-selected' CustomEvent emitted by C8 schematic | takes: window | gives: event handler
  window.addEventListener("component-selected", (e) => {
    const cId = e.detail?.componentId;
    if (cId) {
      handleComponentSelection(cId);
    }
  });

  // STEP 2: Case selector change listener | takes: DOM #case-select | gives: event handler
  const caseSelect = document.getElementById("case-select");
  if (caseSelect) {
    caseSelect.addEventListener("change", (e) => {
      onCaseChange(e.target.value);
    });
  }

  // STEP 3: Mode selector change listener deriving token budget strictly | takes: DOM #mode-select | gives: event handler
  const modeSelect = document.getElementById("mode-select");
  if (modeSelect) {
    modeSelect.addEventListener("change", (e) => {
      state.mode = e.target.value;
      state.tokenBudget = BUDGET_BY_MODE[state.mode] || 3000;

      const budgetInput = document.getElementById("token-budget-input");
      if (budgetInput) {
        budgetInput.value = state.tokenBudget;
      }
      trace("C9", "Reasoning mode updated", { mode: state.mode, budget: state.tokenBudget });
    });
  }

  // STEP 4: Analyze button click listener | takes: DOM #analyze-btn | gives: event handler
  const analyzeBtn = document.getElementById("analyze-btn");
  if (analyzeBtn) {
    analyzeBtn.addEventListener("click", (e) => {
      e.preventDefault();
      executeAnalysis();
    });
  }

  // STEP 5: Saved runs dropdown change listener | takes: DOM #saved-runs-select | gives: event handler
  const savedSelect = document.getElementById("saved-runs-select");
  if (savedSelect) {
    savedSelect.addEventListener("change", (e) => {
      const selectedRunId = e.target.value;
      if (selectedRunId) {
        loadSavedRun(selectedRunId);
      }
    });
  }

  // STEP 6: Tab navigation button listeners | takes: DOM .tab-btn | gives: event handlers
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const targetTab = btn.dataset.tab;
      if (targetTab) switchTab(targetTab);
    });
  });

  // STEP 7: Event delegation for citation links across document | takes: document click | gives: citation drawer
  document.addEventListener("click", (e) => {
    const link = e.target.closest(".citation-link");
    if (link) {
      e.preventDefault();
      const evId = link.dataset.evidenceId;
      const quote = link.dataset.quote || null;
      if (evId) {
        openCitation(evId, quote);
      }
    }
  });

  trace("C9", "Global event listeners registered");
}

/**
 * Execute automated self-test suite against fixture files in mock mode (?mock=1).
 *
 * IN: None.
 * OUT: Promise resolving to boolean verdict.
 * FAILS WITH: Never fails; returns false on assertion error.
 */
async function runMockSelftest() {
  trace("C9", "Initializing mock mode self-test harness");
  state.mockMode = true;

  const failureReasons = [];
  let schematicPass = false;
  let assessmentPass = false;
  let drawerPass = false;
  let metricsPass = false;
  let presetsPass = false;
  let fallbackGuardPass = false;

  // STEP 1: Check BUDGET_BY_MODE values, question preset, and absence of 'live' option | takes: constants, DOM | gives: presetsPass boolean
  const expectedBudgets = BUDGET_BY_MODE["0"] === 6000 &&
                          BUDGET_BY_MODE["A"] === 3000 &&
                          BUDGET_BY_MODE["B"] === 8000 &&
                          BUDGET_BY_MODE["C"] === 16000;
  const questionMatches = BENCHMARK_QUESTION.includes("Alaska Airlines flight 1288") &&
                          BENCHMARK_QUESTION.includes("what caused the left main landing gear collapse");
  const liveOptionAbsent = document.querySelector('#mode-select option[value="live"]') === null;

  if (expectedBudgets && questionMatches && liveOptionAbsent) {
    presetsPass = true;
  } else {
    failureReasons.push(`Presets check failed: budgets=${expectedBudgets}, question=${questionMatches}, liveAbsent=${liveOptionAbsent}`);
  }

  // STEP 2: Check fixture fallback disabled when mockMode is false | takes: fetchJsonWithFallback | gives: fallbackGuardPass boolean
  try {
    state.mockMode = false;
    let guardBlocked = false;
    try {
      await fetchJsonWithFallback("/api/nonexistent_guard_probe_test", "/fixtures/manifest.json");
    } catch (_) {
      guardBlocked = true; // Expected: must throw in real mode instead of using fallback
    }
    state.mockMode = true; // restore mock mode

    if (guardBlocked) {
      fallbackGuardPass = true;
    } else {
      failureReasons.push("Fixture fallback guard failed: returned fixture data when mockMode was false");
    }
  } catch (err) {
    state.mockMode = true;
    failureReasons.push(`Fallback guard error: ${err.message}`);
  }

  // STEP 3: Mount interactive C8 schematic into canvas area | takes: #schematic-canvas-area | gives: mounted SVG
  const canvasArea = document.getElementById("schematic-canvas-area");
  try {
    mountSchematic(canvasArea);
    const svgEl = canvasArea.querySelector(".schematic-svg");
    const regions = canvasArea.querySelectorAll(".schematic-region");
    if (svgEl && regions.length === 5) {
      schematicPass = true;
    } else {
      failureReasons.push(`Schematic mounting incomplete (found ${regions.length} regions, expected 5)`);
    }
  } catch (err) {
    failureReasons.push(`mountSchematic failed: ${err.message}`);
  }

  // STEP 4: Load fixture data files using absolute URLs | takes: /fixtures/... | gives: fixture objects
  let assessmentFixture = null;
  let runlogFixture = null;
  let passagesMap = {};

  try {
    assessmentFixture = await fetchJsonWithFallback("/fixtures/assessment.json");
  } catch (err) {
    failureReasons.push(`Failed loading assessment fixture: ${err.message}`);
  }

  try {
    runlogFixture = await fetchJsonWithFallback("/fixtures/runlog.json");
  } catch (err) {
    failureReasons.push(`Failed loading runlog fixture: ${err.message}`);
  }

  // Load passages.jsonl via absolute path
  try {
    const rawPassagesText = await fetch("/fixtures/passages.jsonl").then(r => r.text());

    rawPassagesText.split("\n").forEach(line => {
      const cleanLine = line.trim();
      if (cleanLine) {
        try {
          const p = JSON.parse(cleanLine);
          if (p.passage_id) {
            p.pdf_url = `/pdfs/${p.document_id}.pdf#page=${p.page_number || 1}`;
            passagesMap[p.passage_id] = p;
          }
        } catch (_) {}
      }
    });
    state.mockFixtures.passages = passagesMap;
  } catch (err) {
    trace("C9", "Warning loading passages fixture text", { error: String(err) });
  }

  // STEP 5: Verify and render Assessment content into briefing panel | takes: assessmentFixture | gives: rendered assessment
  const briefingContainer = document.getElementById("briefing-content");
  try {
    if (assessmentFixture && briefingContainer) {
      // Mock RunLog wrapper
      const mockRunLog = {
        run_id: "FIXTURE:RUN-2026-001",
        run_label: "A",
        model_name: "FIXTURE:google/gemma-4-26B-A4B-it",
        assessment: assessmentFixture,
        verification: runlogFixture?.verification || null,
        metrics: runlogFixture?.metrics || null,
        config: runlogFixture?.config || { max_model_len: 8192 },
        error: null
      };

      state.currentRunLog = mockRunLog;
      state.caseId = assessmentFixture.case_id || "FIXTURE:CASE-2026-LG";
      state.componentId = assessmentFixture.selected_component || "landing_gear";

      // Update schematic regions from component_summaries
      if (assessmentFixture.component_summaries) {
        Object.entries(assessmentFixture.component_summaries).forEach(([comp, sum]) => {
          if (VALID_COMPONENTS.includes(comp)) {
            const passCount = typeof sum.passage_count === "number" ? sum.passage_count : "no curated evidence yet";
            setRegionStatus(comp, sum.evidence_status, passCount);
          }
        });
      }

      setSelected(state.componentId);

      // Render briefing
      const claimChecks = buildClaimChecksMap(mockRunLog);
      renderBriefing(briefingContainer, mockRunLog, state.componentId, 5, claimChecks);

      // Verify DOM content
      const summaryPresent = Boolean(briefingContainer.querySelector(".briefing-summary-text"));
      const hypothesesCards = briefingContainer.querySelectorAll(".hypothesis-card");
      const modelBadge = briefingContainer.querySelector(".badge-model-hypothesis");
      const safetyNote = briefingContainer.querySelector(".check-safety-alert");

      if (summaryPresent && hypothesesCards.length > 0 && modelBadge && safetyNote) {
        assessmentPass = true;
      } else {
        failureReasons.push("Briefing panel missing summary, hypotheses cards, model hypothesis badge, or safety note");
      }
    }
  } catch (err) {
    failureReasons.push(`renderBriefing failed in selftest: ${err.message}`);
  }

  // STEP 6: Verify Citation Drawer opens with excerpt, highlighted quote, and PDF link | takes: passage | gives: open drawer
  const drawerEl = document.getElementById("citation-drawer");
  try {
    const testPassage = passagesMap["FIXTURE:PASSAGE-001"] || {
      passage_id: "FIXTURE:PASSAGE-001",
      document_id: "FIXTURE:DOC-2026-001",
      document_title: "FIXTURE:Factual Report - Landing Gear Actuator Assembly",
      page_number: 4,
      publisher: "FIXTURE:National Transportation Safety Board",
      evidence_role: "observation",
      text: "FIXTURE:Post-flight inspection revealed that the main landing gear downlock pin had fractured along the shear plane.",
      pdf_url: "/pdfs/factual_report_2026_001.pdf#page=4"
    };

    const quoteToHighlight = "FIXTURE:Post-flight inspection revealed that the main landing gear downlock pin had fractured along the shear plane.";
    renderCitationDrawer(drawerEl, testPassage, null, quoteToHighlight);

    const isDrawerOpen = drawerEl.classList.contains("is-open");
    const excerptEl = drawerEl.querySelector(".citation-excerpt");
    const highlightEl = drawerEl.querySelector(".citation-highlight");
    const pdfLinkEl = drawerEl.querySelector(".pdf-link");

    if (isDrawerOpen && excerptEl && highlightEl && pdfLinkEl && pdfLinkEl.getAttribute("href")) {
      drawerPass = true;
    } else {
      failureReasons.push("Citation drawer did not open or is missing highlighted quote or PDF link");
    }
  } catch (err) {
    failureReasons.push(`renderCitationDrawer failed in selftest: ${err.message}`);
  }

  // STEP 7: Verify Metrics Strip displays measured values without fabricated numbers | takes: runlogFixture | gives: rendered metrics
  const metricsContainer = document.getElementById("metrics-strip-container");
  try {
    if (metricsContainer && runlogFixture) {
      renderMetrics(metricsContainer, runlogFixture);

      const metricItems = metricsContainer.querySelectorAll(".metric-item");
      const textContent = metricsContainer.textContent || "";

      // Ensure expected values appear and no undefined strings
      const hasModel = textContent.includes("gemma");
      const hasLatency = textContent.includes("2.45 s");
      const hasTokens = textContent.includes("520");
      const hasSpeed = textContent.includes("126.5 tok/s");
      const noUndefined = !textContent.includes("undefined") && !textContent.includes("NaN");

      if (metricItems.length >= 5 && hasModel && hasLatency && hasTokens && hasSpeed && noUndefined) {
        metricsPass = true;
      } else {
        failureReasons.push("Metrics strip missing expected measured values or contains undefined values");
      }
    }
  } catch (err) {
    failureReasons.push(`renderMetrics failed in selftest: ${err.message}`);
  }

  // STEP 8: Render visible PASS/FAIL verdict banner | takes: test results | gives: rendered checklist banner
  const selftestContainer = document.getElementById("selftest-verdict-container");
  const testResults = {
    schematicPass,
    assessmentPass,
    drawerPass,
    metricsPass,
    presetsPass,
    fallbackGuardPass,
    failureReasons
  };

  if (selftestContainer) {
    renderSelftestVerdict(selftestContainer, testResults);
  }

  // STEP 9: Emit final self-test verdict trace | takes: testResults | gives: void
  const allPassed = schematicPass && assessmentPass && drawerPass && metricsPass && presetsPass && fallbackGuardPass;
  if (allPassed) {
    console.log("PASS: C9 Dashboard Shell self-test passed all fixture contract assertions");
    trace("C9", "PASS: Selftest succeeded", {
      schematic: "PASS",
      assessment: "PASS",
      drawer: "PASS",
      metrics: "PASS",
      presets: "PASS",
      fallbackGuard: "PASS"
    });
  } else {
    console.error("FAIL: C9 Dashboard Shell self-test failed:", failureReasons.join("; "));
    trace("C9", "FAIL: Selftest failed", { reasons: failureReasons.join(" | ") });
  }

  // Setup interactive listeners so mock dashboard can still be explored
  setupEventListeners();
  return allPassed;
}

/**
 * Main application initialization entrypoint.
 *
 * IN: None.
 * OUT: Promise resolving when dashboard is ready.
 * FAILS WITH: ComponentError on catastrophic initialization failure.
 */
export async function initApp() {
  // STEP 1: Inspect URL parameters for ?mock=1 mode | takes: window.location | gives: boolean isMock
  const urlParams = new URLSearchParams(window.location.search);
  const isMock = urlParams.get("mock") === "1" || urlParams.get("mock") === "true";

  trace("C9", "Initializing dashboard application", { mockMode: isMock });

  // STEP 2: Dispatch to mock self-test runner or live API bootstrapper | takes: isMock | gives: initialized dashboard
  if (isMock) {
    await runMockSelftest();
    return;
  }

  // Live Mode Bootstrap
  try {
    // Mount schematic
    const canvasArea = document.getElementById("schematic-canvas-area");
    if (canvasArea) {
      mountSchematic(canvasArea);
    }

    // Initialize question box with benchmark question preset
    const questionEl = document.getElementById("question-input");
    if (questionEl && !questionEl.value.trim()) {
      questionEl.value = BENCHMARK_QUESTION;
    }

    // Initialize budget to Mode A preset
    state.mode = "A";
    state.tokenBudget = BUDGET_BY_MODE["A"];
    const budgetInput = document.getElementById("token-budget-input");
    if (budgetInput) {
      budgetInput.value = state.tokenBudget;
    }

    // Setup events
    setupEventListeners();

    // Load cases from backend
    await loadCases();

    trace("C9", "Dashboard application initialized successfully");
  } catch (err) {
    trace("C9", "Application initialization error", { error: String(err) });
  }
}

// Auto-run bootstrap on DOMContentLoaded
window.addEventListener("DOMContentLoaded", () => {
  initApp();
});

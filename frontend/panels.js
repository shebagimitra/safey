// ============================================================
// COMPONENT: C9 - Briefing and Evidence Panels
// PURPOSE: Renders briefing summaries, hypotheses, documented findings, citation drawer, metrics strip, and evaluation views.
// INPUT: Container HTMLElements, RunLog objects, Passage models, verification claims, and status descriptors.
// OUTPUT: Populated DOM structures, formatted badges, interactive citation drawers, and evaluation comparison views.
// CALLED BY: frontend/app.js      CALLS: DOM APIs, common/trace conventions
// IF IT BREAKS, CHECK: Null runLog/assessment handling, DOM selector IDs, citation event listeners, PDF link schemas.
// ============================================================

/**
 * Standard diagnostic component error matching system conventions.
 */
export class ComponentError extends Error {
  /**
   * IN: component (string), step (string), message (string).
   * OUT: ComponentError exception instance.
   * FAILS WITH: Never fails initialization.
   */
  constructor(component, step, message) {
    // STEP 1: Format standardized diagnostic message | takes: component, step, message | gives: formatted message
    super(`[${component}] step '${step}' failed: ${message}`);
    this.name = "ComponentError";
    this.component = component;
    this.step = step;
    this.message = message;
  }
}

/**
 * Diagnostic trace logger emitting structured telemetry to console.
 *
 * IN: component (string), message (string), fields (object).
 * OUT: void.
 * FAILS WITH: Never fails.
 */
export function trace(component, message, fields = {}) {
  // STEP 1: Format UTC timestamp and serialize key-value fields | takes: component, message, fields | gives: log string
  const timestamp = new Date().toISOString();
  const fieldList = Object.entries(fields).map(([k, v]) => `${k}=${v}`);
  const logLine = fieldList.length > 0
    ? `[${timestamp}] [${component}] ${message} | ${fieldList.join(" ")}`
    : `[${timestamp}] [${component}] ${message}`;

  // STEP 2: Output trace line to console | takes: logLine | gives: stdout/console entry
  console.log(logLine);
}

/**
 * Safely escape plain text strings for safe injection into innerHTML.
 *
 * IN: str (string or any).
 * OUT: HTML-escaped string.
 * FAILS WITH: Never fails; returns empty string for null/undefined.
 */
export function escapeHtml(str) {
  // STEP 1: Coerce to string and replace HTML reserved characters | takes: str | gives: safe string
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/**
 * Render visual badge for evidence roles with distinct styling.
 *
 * IN: role (string).
 * OUT: HTML string for role badge.
 * FAILS WITH: Never fails.
 */
export function getRoleBadge(role) {
  // STEP 1: Map evidence role to badge class and human label | takes: role | gives: HTML string
  const normRole = (role || "").toLowerCase().trim();
  switch (normRole) {
    case "official_finding":
      return `<span class="badge badge-role badge-role-official" title="Documented official finding by investigating authority">Official Finding</span>`;
    case "observation":
      return `<span class="badge badge-role badge-role-observation" title="Factual inspection or physical observation">Observation</span>`;
    case "guidance":
      return `<span class="badge badge-role badge-role-guidance" title="Advisory Circular or regulatory guidance">Guidance</span>`;
    case "recommendation":
      return `<span class="badge badge-role badge-role-recommendation" title="Safety recommendation">Recommendation</span>`;
    case "corrective_action":
      return `<span class="badge badge-role badge-role-corrective" title="Documented mandatory corrective action">Corrective Action</span>`;
    case "spoken_statement":
      return `<span class="badge badge-role badge-role-spoken" title="Witness or crew spoken statement">Spoken Statement</span>`;
    case "model_inference":
      return `<span class="badge badge-role badge-role-inference" title="Model derived inference">Model Inference</span>`;
    default:
      return `<span class="badge badge-role badge-role-default">${escapeHtml(role || "Evidence")}</span>`;
  }
}

/**
 * Render evidence status chip matching Section 12.4 color conventions.
 *
 * IN: status (string).
 * OUT: HTML string for status chip.
 * FAILS WITH: Never fails.
 */
export function getStatusChip(status) {
  // STEP 1: Normalize status string and assign Section 12.4 color class | takes: status | gives: HTML string
  const norm = (status || "unassessed").toLowerCase().trim();
  let colorClass = "chip-gray";
  let label = norm;

  if (norm === "supported" || norm === "red") {
    colorClass = "chip-red";
    label = "Supported (Documented Concern)";
  } else if (norm === "mixed" || norm === "contradictory" || norm === "amber") {
    colorClass = "chip-amber";
    label = norm === "contradictory" ? "Contradictory Evidence" : "Mixed Evidence";
  } else if (norm === "historical" || norm === "blue") {
    colorClass = "chip-blue";
    label = "Historical / Reference";
  } else if (norm === "corrective" || norm === "green") {
    colorClass = "chip-green";
    label = "Verified Corrective Action";
  } else {
    colorClass = "chip-gray";
    label = norm === "insufficient" ? "Insufficient Evidence" : "Unassessed";
  }

  return `<span class="status-chip ${colorClass}">${escapeHtml(label)}</span>`;
}

/**
 * Highlight cited quote within passage excerpt text.
 *
 * IN: fullText (string), quote (string).
 * OUT: HTML string containing highlighted quote segments.
 * FAILS WITH: Never fails.
 */
export function highlightQuote(fullText, quote) {
  // STEP 1: Validate text input and normalize parameters | takes: fullText, quote | gives: strings
  if (!fullText) return "";
  const escFull = escapeHtml(fullText);
  if (!quote) return escFull;

  const cleanQuote = quote.trim();
  const escQuote = escapeHtml(cleanQuote);

  // STEP 2: Check for direct contiguous match | takes: escFull, escQuote | gives: highlighted HTML or fallback
  if (escFull.includes(escQuote)) {
    return escFull.replace(escQuote, `<mark class="citation-highlight">${escQuote}</mark>`);
  }

  // STEP 3: Handle segmented quotes containing ellipses | takes: cleanQuote | gives: segmented matches
  const segments = cleanQuote.split(/\s*\.{3}\s*/).map(s => s.trim()).filter(s => s.length > 4);
  let highlighted = escFull;
  let matchCount = 0;

  for (const seg of segments) {
    const escSeg = escapeHtml(seg);
    if (highlighted.includes(escSeg)) {
      highlighted = highlighted.replace(escSeg, `<mark class="citation-highlight">${escSeg}</mark>`);
      matchCount++;
    }
  }

  if (matchCount > 0) {
    return highlighted;
  }

  // STEP 4: Fallback banner if exact substring not located in passage text | takes: cleanQuote | gives: annotated HTML
  return `<div class="quote-lookup-notice"><strong>Cited Quote:</strong> &ldquo;${escQuote}&rdquo;</div>${escFull}`;
}

/**
 * Render generic key/value tree for arbitrary JSON facts object.
 *
 * IN: data (any primitive, array, or object).
 * OUT: HTML string representing nested tree.
 * FAILS WITH: Never fails.
 */
export function renderJsonTree(data) {
  // STEP 1: Handle null, undefined, and primitive types | takes: data | gives: HTML string
  if (data === null || data === undefined) {
    return `<span class="tree-null">null</span>`;
  }
  if (typeof data !== "object") {
    return `<span class="tree-value">${escapeHtml(String(data))}</span>`;
  }
  // STEP 2: Handle array elements recursively | takes: data | gives: list HTML
  if (Array.isArray(data)) {
    if (data.length === 0) return `<span class="tree-empty">[]</span>`;
    const items = data.map((item, idx) => `
      <li class="tree-list-item">
        <span class="tree-index">[${idx}]:</span> ${renderJsonTree(item)}
      </li>
    `).join("");
    return `<ul class="tree-list">${items}</ul>`;
  }
  // STEP 3: Handle dictionary key/value pairs recursively | takes: data | gives: dl HTML
  const keys = Object.keys(data);
  if (keys.length === 0) return `<span class="tree-empty">{}</span>`;
  const rows = keys.map(k => `
    <div class="tree-row">
      <dt class="tree-key">${escapeHtml(k)}:</dt>
      <dd class="tree-val">${renderJsonTree(data[k])}</dd>
    </div>
  `).join("");
  return `<dl class="tree-dict">${rows}</dl>`;
}

/**
 * Render collapsible Evolus document overview block with bullet verification markers and facts tree.
 *
 * IN: overviewData (object or null).
 * OUT: HTML string for document overview section or empty string.
 * FAILS WITH: Never fails; returns empty string if overviewData is null.
 */
export function renderEvolusOverview(overviewData) {
  // STEP 1: Validate overview data existence | takes: overviewData | gives: validated data or empty string
  if (!overviewData || (!overviewData.summary && !overviewData.facts)) {
    return "";
  }

  // STEP 2: Render summary bullets with figure verification markers | takes: overviewData.summary | gives: bullets HTML
  let bulletsHtml = "";
  const bullets = Array.isArray(overviewData.summary?.bullets) ? overviewData.summary.bullets : [];
  if (bullets.length > 0) {
    const items = bullets.map(b => {
      const nums = Array.isArray(b.numbers) ? b.numbers : [];
      let markerHtml = "";
      if (nums.length > 0) {
        const missing = nums.filter(n => !n.found).map(n => n.token);
        if (missing.length === 0) {
          markerHtml = `<span class="evolus-figure-marker marker-verified">&#x2713; all ${nums.length} figures found in source text</span>`;
        } else {
          markerHtml = `<span class="evolus-figure-marker marker-missing">&#x26A0; missing figures: ${escapeHtml(missing.join(", "))}</span>`;
        }
      }
      return `
        <li class="evolus-bullet-item">
          <span class="evolus-bullet-text">${escapeHtml(b.text || "")}</span>
          ${markerHtml}
        </li>
      `;
    }).join("");
    bulletsHtml = `
      <div class="evolus-summary-section">
        <ul class="evolus-bullets-list">${items}</ul>
      </div>
    `;
  }

  // STEP 3: Render collapsed Extracted case facts section with numbers found/checked counter | takes: overviewData.facts | gives: facts HTML
  let factsHtml = "";
  if (overviewData.facts && overviewData.facts.data) {
    const checked = overviewData.facts.numbers_checked ?? 0;
    const found = overviewData.facts.numbers_found ?? 0;
    const missingArr = Array.isArray(overviewData.facts.missing) ? overviewData.facts.missing : [];
    const missingSummary = missingArr.length > 0 ? ` &bull; missing: ${escapeHtml(missingArr.join(", "))}` : "";
    const counterText = `${found} / ${checked} figures found in source text${missingSummary}`;

    factsHtml = `
      <details class="evolus-facts-details">
        <summary class="evolus-facts-summary">
          <span class="facts-summary-title">Extracted case facts</span>
          <span class="evolus-facts-counter">(${escapeHtml(counterText)})</span>
        </summary>
        <div class="evolus-tree-container">
          ${renderJsonTree(overviewData.facts.data)}
        </div>
      </details>
    `;
  }

  // STEP 4: Assemble collapsible overview block with mandatory fixed disclaimer note | takes: bulletsHtml, factsHtml | gives: complete HTML block
  const fixedNoteText = "Generated by Evolus. The check confirms figures appear in the excerpt text, not that a sentence is correct. Page citations come only from the verified findings below.";
  const docTitleSuffix = overviewData.document_title ? ` &mdash; ${escapeHtml(overviewData.document_title)}` : "";

  return `
    <div class="panel-section evolus-panel-section">
      <details class="evolus-overview-card" open>
        <summary class="evolus-overview-summary">
          <span class="evolus-card-title">Document overview (Evolus, generated, not verified)</span>
          <span class="evolus-card-subtitle">${docTitleSuffix}</span>
        </summary>
        <div class="evolus-overview-body">
          ${bulletsHtml}
          ${factsHtml}
          <p class="evolus-fixed-note">${escapeHtml(fixedNoteText)}</p>
        </div>
      </details>
    </div>
  `;
}

/**
 * Render the briefing panel displaying summary, hypotheses, findings, and limitations.
 *
 * IN: containerEl (HTMLElement), runLog (object or null), componentId (string), countsData (number or string), claimChecksMap (object), evolusOverview (object or null).
 * OUT: void.
 * FAILS WITH: ComponentError if containerEl is invalid.
 */
export function renderBriefing(containerEl, runLog, componentId, countsData, claimChecksMap = {}, evolusOverview = null) {
  // STEP 1: Validate target container element | takes: containerEl | gives: verified element
  if (!containerEl || typeof containerEl.innerHTML === "undefined") {
    throw new ComponentError("C9", "renderBriefing", "Invalid containerEl provided; must be a valid DOM element");
  }

  trace("C9", "Rendering briefing panel", {
    componentId,
    hasRunLog: Boolean(runLog),
    runLabel: runLog?.run_label || "none"
  });

  // STEP 2: Render persistent limitations header notice | takes: None | gives: disclaimer HTML
  const persistentNoticeHtml = `
    <div class="persistent-notice" role="note" aria-label="Investigation disclaimer">
      <span class="notice-icon">&#x26A0;</span>
      <span class="notice-text"><strong>Investigation support only.</strong> Not an airworthiness or maintenance decision.</span>
    </div>
  `;

  // STEP 3: Handle API Error state | takes: runLog.error | gives: error panel HTML
  if (runLog && runLog.error) {
    const errObj = typeof runLog.error === "object" ? runLog.error : { message: String(runLog.error) };
    const errComp = errObj.component || "C7";
    const errStep = errObj.step || "analyze";
    const errMsg = errObj.message || "An unexpected error occurred during investigation analysis.";

    containerEl.innerHTML = `
      ${persistentNoticeHtml}
      <div class="panel-section error-card" role="alert">
        <div class="error-header">
          <span class="error-badge">Component Error: [${escapeHtml(errComp)}]</span>
          <span class="error-step">Step: ${escapeHtml(errStep)}</span>
        </div>
        <p class="error-message">${escapeHtml(errMsg)}</p>
        <p class="error-hint">Target Component: <strong>${escapeHtml(componentId || "None")}</strong></p>
      </div>
    `;
    return;
  }

  // STEP 4: Handle Unsupported Component or No Curated Evidence State | takes: countsData | gives: empty state HTML
  if (countsData === "no curated evidence yet" && (!runLog || !runLog.assessment)) {
    containerEl.innerHTML = `
      ${persistentNoticeHtml}
      <div class="panel-section empty-evidence-card">
        <div class="empty-header">
          <h3>${escapeHtml(componentId || "Component")}</h3>
          <span class="status-chip chip-gray">no curated evidence yet</span>
        </div>
        <p class="empty-text">No prediction passages or curated evidence have been tagged for this component in the current case corpus.</p>
        <p class="empty-subtext">The frontend never infers aviation findings when evidence is absent.</p>
      </div>
    `;
    return;
  }

  // STEP 5: Handle Mode 0 Baseline (assessment is null): plain-text answer without citation UI | takes: runLog | gives: baseline HTML
  if (runLog && (runLog.assessment === null || runLog.run_label === "0")) {
    const rawText = runLog.raw_output || "(No raw output generated)";
    containerEl.innerHTML = `
      ${persistentNoticeHtml}
      <div class="panel-section baseline-card">
        <div class="section-header">
          <h3 class="baseline-title">Baseline: model memory only, no documents, no citations</h3>
          <span class="status-chip chip-gray">Run 0 Baseline</span>
        </div>
        <p class="baseline-note">This baseline run was executed without retrieved corpus documents to evaluate pre-trained parametric memory only. No citations or evidence passages are associated with this output.</p>
        <div class="baseline-raw-output">
          <div class="baseline-text">${escapeHtml(rawText)}</div>
        </div>
      </div>
    `;
    return;
  }

  // STEP 6: If no runLog or assessment available yet, render prompt state | takes: runLog | gives: prompt HTML
  if (!runLog || !runLog.assessment) {
    const formattedCounts = typeof countsData === "number" ? `${countsData} tagged passages` : "no curated evidence yet";
    const evolusOverviewHtml = renderEvolusOverview(evolusOverview);
    containerEl.innerHTML = `
      ${persistentNoticeHtml}
      ${evolusOverviewHtml}
      <div class="panel-section initial-prompt-card">
        <h3>System Ready</h3>
        <p>Select an aircraft component from the schematic or control list and click <strong>Analyze Component</strong> to inspect findings.</p>
        <p class="text-muted">Tagged passages: <strong>${escapeHtml(formattedCounts)}</strong></p>
      </div>
    `;
    return;
  }

  const assessment = runLog.assessment;

  // STEP 7: Build Brief Summary & Evidence Status Header | takes: assessment | gives: summary HTML
  const summaryHtml = `
    <div class="briefing-header-block">
      <div class="briefing-title-row">
        <h3>${escapeHtml(assessment.selected_component || componentId || "Component Analysis")}</h3>
        ${getStatusChip(assessment.evidence_status)}
      </div>
      <p class="briefing-summary-text">${escapeHtml(assessment.brief_summary || "No summary provided.")}</p>
    </div>
  `;

  // STEP 8: Build Hypotheses Section | takes: assessment.hypotheses, claimChecksMap | gives: hypotheses HTML
  let hypothesesHtml = "";
  const hypotheses = Array.isArray(assessment.hypotheses) ? assessment.hypotheses : [];
  if (hypotheses.length > 0) {
    const items = hypotheses.map((hyp, index) => {
      // Supporting citations
      const supCitations = (hyp.supporting_evidence || []).map(ev => {
        const check = claimChecksMap[ev.evidence_id];
        const isFlagged = check && (check.downgraded || check.quote_found === false || check.reason);
        const warningPill = isFlagged
          ? `<span class="badge-needs-review" title="${escapeHtml(check.reason || 'Flagged by verification')}">&#x26A0; needs review</span>`
          : "";
        return `
          <li class="citation-item">
            <a href="#" class="citation-link" data-evidence-id="${escapeHtml(ev.evidence_id)}" data-quote="${escapeHtml(ev.quote || '')}">
              ${escapeHtml(ev.evidence_id)}
            </a>
            ${warningPill}
            <span class="citation-quote-preview">&ldquo;${escapeHtml(ev.quote || "")}&rdquo;</span>
          </li>
        `;
      }).join("");

      // Contradicting citations
      const conCitations = (hyp.contradicting_evidence || []).map(ev => {
        const check = claimChecksMap[ev.evidence_id];
        const isFlagged = check && (check.downgraded || check.quote_found === false || check.reason);
        const warningPill = isFlagged
          ? `<span class="badge-needs-review" title="${escapeHtml(check.reason || 'Flagged by verification')}">&#x26A0; needs review</span>`
          : "";
        return `
          <li class="citation-item contradiction">
            <a href="#" class="citation-link" data-evidence-id="${escapeHtml(ev.evidence_id)}" data-quote="${escapeHtml(ev.quote || '')}">
              ${escapeHtml(ev.evidence_id)}
            </a>
            ${warningPill}
            <span class="citation-quote-preview">&ldquo;${escapeHtml(ev.quote || "")}&rdquo;</span>
          </li>
        `;
      }).join("");

      // Limitations
      const limitationsList = (hyp.limitations || []).map(l => `<li>${escapeHtml(l)}</li>`).join("");

      return `
        <div class="hypothesis-card">
          <div class="hypothesis-meta-row">
            <span class="badge badge-model-hypothesis">Model hypothesis</span>
            <span class="hypothesis-status-pill ${escapeHtml(hyp.status || 'plausible')}">${escapeHtml(hyp.status || "plausible")}</span>
            <span class="hypothesis-id">${escapeHtml(hyp.id || `H${index + 1}`)}</span>
          </div>
          <p class="hypothesis-statement">${escapeHtml(hyp.statement || "")}</p>
          <div class="hypothesis-detail-group">
            <span class="detail-label">Reasoning:</span>
            <p class="hypothesis-reasoning">${escapeHtml(hyp.reasoning || "None stated")}</p>
          </div>
          ${supCitations ? `
            <div class="evidence-subgroup">
              <span class="detail-label">Supporting Evidence:</span>
              <ul class="citation-list">${supCitations}</ul>
            </div>
          ` : ""}
          ${conCitations ? `
            <div class="evidence-subgroup">
              <span class="detail-label contradiction-label">Contradicting / Inconsistent Evidence:</span>
              <ul class="citation-list">${conCitations}</ul>
            </div>
          ` : ""}
          ${limitationsList ? `
            <div class="evidence-subgroup">
              <span class="detail-label">Hypothesis Limitations:</span>
              <ul class="limitations-list">${limitationsList}</ul>
            </div>
          ` : ""}
        </div>
      `;
    }).join("");

    hypothesesHtml = `
      <div class="panel-section">
        <h4 class="section-title">Hypotheses Evaluation</h4>
        <div class="hypotheses-list">${items}</div>
      </div>
    `;
  }

  // STEP 8b: Build Evolus Document Overview Block (collapsible, placed above Documented findings) | takes: evolusOverview | gives: overview HTML
  const evolusOverviewHtml = renderEvolusOverview(evolusOverview);

  // STEP 9: Build Documented Findings Section | takes: assessment.documented_findings, claimChecksMap | gives: findings HTML
  let findingsHtml = "";
  const findings = Array.isArray(assessment.documented_findings) ? assessment.documented_findings : [];
  if (findings.length > 0) {
    const items = findings.map(f => {
      const check = claimChecksMap[f.evidence_id];
      const isFlagged = check && (check.downgraded || check.quote_found === false || check.reason);
      const warningPill = isFlagged
        ? `<span class="badge-needs-review" title="${escapeHtml(check.reason || 'Flagged by verification')}">&#x26A0; needs review: ${escapeHtml(check.reason || 'citation mismatch')}</span>`
        : "";

      return `
        <div class="finding-card">
          <div class="finding-header-row">
            ${getRoleBadge(f.source_role)}
            <a href="#" class="citation-link" data-evidence-id="${escapeHtml(f.evidence_id)}" data-quote="${escapeHtml(f.quote || '')}">
              ${escapeHtml(f.evidence_id)}
            </a>
            ${warningPill}
          </div>
          <p class="finding-statement">${escapeHtml(f.statement || "")}</p>
          <blockquote class="finding-quote">&ldquo;${escapeHtml(f.quote || "")}&rdquo;</blockquote>
        </div>
      `;
    }).join("");

    findingsHtml = `
      <div class="panel-section">
        <h4 class="section-title">Documented Findings</h4>
        <div class="findings-list">${items}</div>
      </div>
    `;
  }

  // STEP 10: Build Missing Information Section | takes: assessment.missing_information | gives: missing info HTML
  let missingInfoHtml = "";
  const missing = Array.isArray(assessment.missing_information) ? assessment.missing_information : [];
  if (missing.length > 0) {
    const items = missing.map(m => `<li>${escapeHtml(m)}</li>`).join("");
    missingInfoHtml = `
      <div class="panel-section">
        <h4 class="section-title">Missing Information &amp; Unknowns</h4>
        <ul class="missing-info-list">${items}</ul>
      </div>
    `;
  }

  // STEP 11: Build Suggested Checks Section with Mandatory Safety Note | takes: assessment.suggested_investigative_checks | gives: checks HTML
  let checksHtml = "";
  const checks = Array.isArray(assessment.suggested_investigative_checks) ? assessment.suggested_investigative_checks : [];
  if (checks.length > 0) {
    const items = checks.map(c => {
      const citations = (c.evidence_ids || []).map(id => `
        <a href="#" class="citation-link" data-evidence-id="${escapeHtml(id)}">${escapeHtml(id)}</a>
      `).join(", ");

      const safetyNoteText = c.safety_note || "For qualified maintenance personnel only; not an airworthiness directive.";

      return `
        <div class="check-card">
          <p class="check-instruction"><strong>Check:</strong> ${escapeHtml(c.check || "")}</p>
          <p class="check-rationale"><strong>Rationale:</strong> ${escapeHtml(c.rationale || "")}</p>
          ${citations ? `<p class="check-citations"><strong>Referenced Evidence:</strong> ${citations}</p>` : ""}
          <div class="check-safety-alert" role="alert">
            <span class="safety-icon">&#x26A0;</span>
            <span class="safety-text"><strong>Safety Note:</strong> ${escapeHtml(safetyNoteText)}</span>
          </div>
        </div>
      `;
    }).join("");

    checksHtml = `
      <div class="panel-section">
        <h4 class="section-title">Suggested Investigative Checks</h4>
        <div class="checks-list">${items}</div>
      </div>
    `;
  }

  // STEP 12: Build Overall Limitations Banner | takes: assessment.overall_limitations | gives: limitations HTML
  let overallLimitationsHtml = "";
  const overallLimits = Array.isArray(assessment.overall_limitations) ? assessment.overall_limitations : [];
  if (overallLimits.length > 0) {
    const items = overallLimits.map(l => `<li>${escapeHtml(l)}</li>`).join("");
    overallLimitationsHtml = `
      <div class="panel-section limitations-banner">
        <h4 class="limitations-title">&#x26A0; Overall Investigation Limitations</h4>
        <ul class="limitations-list">${items}</ul>
      </div>
    `;
  }

  // STEP 13: Assemble full briefing markup into container | takes: HTML fragments | gives: rendered panel
  containerEl.innerHTML = `
    ${persistentNoticeHtml}
    ${summaryHtml}
    ${hypothesesHtml}
    ${evolusOverviewHtml}
    ${findingsHtml}
    ${missingInfoHtml}
    ${checksHtml}
    ${overallLimitationsHtml}
  `;

  trace("C9", "Briefing panel rendering complete", {
    hypothesesCount: hypotheses.length,
    findingsCount: findings.length,
    checksCount: checks.length
  });
}

/**
 * Render measured telemetry metrics strip (never fabricate numbers).
 *
 * IN: containerEl (HTMLElement), runLog (object or null).
 * OUT: void.
 * FAILS WITH: ComponentError if containerEl is invalid.
 */
export function renderMetrics(containerEl, runLog) {
  // STEP 1: Validate containerEl | takes: containerEl | gives: verified element
  if (!containerEl || typeof containerEl.innerHTML === "undefined") {
    throw new ComponentError("C9", "renderMetrics", "Invalid containerEl provided; must be a valid DOM element");
  }

  // STEP 2: Extract verified measurements, displaying 'not measured' when absent | takes: runLog | gives: metric values
  const metrics = runLog?.metrics || {};
  const config = runLog?.config || {};

  const modelName = runLog?.model_name || "not measured";
  const latency = (metrics.latency_s !== null && metrics.latency_s !== undefined)
    ? `${Number(metrics.latency_s).toFixed(2)} s`
    : "not measured";
  const inputTokens = (metrics.input_tokens !== null && metrics.input_tokens !== undefined)
    ? `${metrics.input_tokens}`
    : "not measured";
  const outputTokens = (metrics.output_tokens !== null && metrics.output_tokens !== undefined)
    ? `${metrics.output_tokens}`
    : "not measured";
  const tokensPerSec = (metrics.tokens_per_s !== null && metrics.tokens_per_s !== undefined)
    ? `${Number(metrics.tokens_per_s).toFixed(1)} tok/s`
    : "not measured";

  // max_model_len 0 means 'not measured' per specification
  const maxModelLen = (config.max_model_len && Number(config.max_model_len) > 0)
    ? `${config.max_model_len}`
    : "not measured";

  // STEP 3: Render metrics cards strip | takes: metric values | gives: HTML rendered
  containerEl.innerHTML = `
    <div class="metrics-grid">
      <div class="metric-item">
        <span class="metric-label">Model</span>
        <span class="metric-value model-name" title="${escapeHtml(modelName)}">${escapeHtml(modelName)}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Latency</span>
        <span class="metric-value">${escapeHtml(latency)}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Input Tokens</span>
        <span class="metric-value">${escapeHtml(inputTokens)}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Output Tokens</span>
        <span class="metric-value">${escapeHtml(outputTokens)}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Speed</span>
        <span class="metric-value">${escapeHtml(tokensPerSec)}</span>
      </div>
      <div class="metric-item">
        <span class="metric-label">Context Size</span>
        <span class="metric-value">${escapeHtml(maxModelLen)}</span>
      </div>
    </div>
  `;

  trace("C9", "Metrics strip rendered", {
    latency,
    inputTokens,
    outputTokens,
    speed: tokensPerSec
  });
}

/**
 * Render citation drawer contents with passage excerpt, highlighted quote, and verified PDF link.
 *
 * IN: drawerEl (HTMLElement), passage (object), claimCheck (object or null), citedQuote (string or null).
 * OUT: void.
 * FAILS WITH: ComponentError if drawerEl or passage is invalid.
 */
export function renderCitationDrawer(drawerEl, passage, claimCheck = null, citedQuote = null) {
  // STEP 1: Validate drawer and passage arguments | takes: drawerEl, passage | gives: validated parameters
  if (!drawerEl || typeof drawerEl.innerHTML === "undefined") {
    throw new ComponentError("C9", "renderCitationDrawer", "Invalid drawerEl provided; must be a valid DOM element");
  }
  if (!passage || typeof passage !== "object") {
    throw new ComponentError("C9", "renderCitationDrawer", "Invalid passage object provided");
  }

  trace("C9", "Rendering citation drawer", {
    passageId: passage.passage_id,
    page: passage.page_number,
    hasQuote: Boolean(citedQuote)
  });

  // STEP 2: Format citation header fields | takes: passage | gives: metadata strings
  const title = passage.document_title || passage.document_id || "Document Passage";
  const page = (passage.page_number !== null && passage.page_number !== undefined)
    ? `Page ${passage.page_number}`
    : "Page N/A";
  const authority = passage.publisher || "Investigating Authority";
  const roleBadge = getRoleBadge(passage.evidence_role);

  // STEP 3: Check verification review flag | takes: claimCheck | gives: review banner HTML
  let reviewAlertHtml = "";
  if (claimCheck && (claimCheck.downgraded || claimCheck.quote_found === false || claimCheck.reason)) {
    const reasonText = claimCheck.reason || "Verification warning: quote or passage reference downgraded";
    reviewAlertHtml = `
      <div class="drawer-review-alert" role="alert">
        <span class="alert-icon">&#x26A0;</span>
        <div class="alert-content">
          <strong>Citation Flagged (Needs Review):</strong>
          <span>${escapeHtml(reasonText)}</span>
        </div>
      </div>
    `;
  }

  // STEP 4: Format excerpt text with highlighted quote | takes: passage.text, citedQuote | gives: highlighted HTML
  const excerptHtml = highlightQuote(passage.text || "(No passage text content available)", citedQuote);

  // STEP 5: Prepare PDF link (per addendum: open pdf_url, not source_url) | takes: passage.pdf_url | gives: link HTML
  const pdfUrl = passage.pdf_url || `/pdfs/${passage.document_id}.pdf#page=${passage.page_number || 1}`;
  const pdfLinkHtml = `
    <a href="${escapeHtml(pdfUrl)}" target="_blank" rel="noopener noreferrer" class="btn btn-primary pdf-link">
      <span class="btn-icon">&#x1F4C4;</span> Open original PDF page
    </a>
  `;

  // STEP 6: Populate drawer HTML | takes: assembled fragments | gives: rendered drawer
  drawerEl.innerHTML = `
    <div class="drawer-header">
      <div class="drawer-title-group">
        <span class="drawer-passage-id">${escapeHtml(passage.passage_id || "")}</span>
        <h3 class="drawer-title">${escapeHtml(title)}</h3>
        <div class="drawer-meta-pills">
          <span class="meta-pill">${escapeHtml(page)}</span>
          <span class="meta-pill">${escapeHtml(authority)}</span>
          ${roleBadge}
        </div>
      </div>
      <button type="button" class="btn-close-drawer" aria-label="Close citation drawer">&times;</button>
    </div>
    <div class="drawer-body">
      ${reviewAlertHtml}
      <div class="drawer-section">
        <h4 class="drawer-section-title">Passage Excerpt</h4>
        <div class="citation-excerpt">${excerptHtml}</div>
      </div>
      <div class="drawer-section">
        <h4 class="drawer-section-title">Primary Source Document</h4>
        <div class="drawer-source-actions">
          ${pdfLinkHtml}
          ${passage.source_url ? `
            <a href="${escapeHtml(passage.source_url)}" target="_blank" rel="noopener noreferrer" class="btn btn-secondary source-url-link">
              External Reference URL
            </a>
          ` : ""}
        </div>
      </div>
    </div>
  `;

  // STEP 7: Ensure drawer is visibly open and attach close listener | takes: drawerEl | gives: active drawer
  drawerEl.classList.add("is-open");
  const closeBtn = drawerEl.querySelector(".btn-close-drawer");
  if (closeBtn) {
    closeBtn.onclick = () => {
      drawerEl.classList.remove("is-open");
    };
  }
}

/**
 * Render the Evaluation view listing case runs and official ground truth reveal.
 *
 * IN: containerEl (HTMLElement), runsList (array), officialConclusion (string or null), latestPrediction (string or null), onRevealClick (function), caseId (string).
 * OUT: void.
 * FAILS WITH: ComponentError if containerEl is invalid.
 */
export function renderEvaluationView(containerEl, runsList = [], officialConclusion = null, latestPrediction = null, onRevealClick = null, caseId = "") {
  // STEP 1: Validate containerEl | takes: containerEl | gives: verified element
  if (!containerEl || typeof containerEl.innerHTML === "undefined") {
    throw new ComponentError("C9", "renderEvaluationView", "Invalid containerEl provided; must be a valid DOM element");
  }

  trace("C9", "Rendering evaluation view", {
    caseId,
    runsCount: runsList.length,
    hasOfficialConclusion: Boolean(officialConclusion)
  });

  // STEP 2: Format runs list table rows | takes: runsList | gives: table rows HTML
  const rows = (runsList || []).map(run => {
    const runId = run.run_id || "run-unknown";
    const label = run.run_label || "A";
    const model = run.model_name || "not measured";
    const inTokens = run.metrics?.input_tokens ?? "N/A";
    const latency = run.metrics?.latency_s ? `${Number(run.metrics.latency_s).toFixed(2)}s` : "N/A";

    let verifStatus = run.verification?.status || "unverified";
    let verifClass = "verif-unverified";
    if (run.error) {
      verifStatus = "error";
      verifClass = "verif-error";
    } else if (verifStatus === "verified") {
      verifClass = "verif-verified";
    } else if (verifStatus === "partial") {
      verifClass = "verif-partial";
    }

    return `
      <tr>
        <td><span class="run-label-badge label-${escapeHtml(label)}">${escapeHtml(label)}</span></td>
        <td class="font-mono">${escapeHtml(runId)}</td>
        <td>${escapeHtml(model)}</td>
        <td>${escapeHtml(String(inTokens))}</td>
        <td>${escapeHtml(latency)}</td>
        <td><span class="verif-chip ${verifClass}">${escapeHtml(verifStatus)}</span></td>
        <td>
          <button type="button" class="btn btn-sm btn-load-run" data-run-id="${escapeHtml(runId)}">View</button>
        </td>
      </tr>
    `;
  }).join("");

  const runsTableHtml = rows ? `
    <table class="runs-table">
      <thead>
        <tr>
          <th>Mode</th>
          <th>Run ID</th>
          <th>Model</th>
          <th>Tokens In</th>
          <th>Latency</th>
          <th>Verification</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  ` : `<p class="empty-text">No evaluation runs logged for case <strong>${escapeHtml(caseId)}</strong> yet.</p>`;

  // STEP 3: Format official conclusion reveal side-by-side section | takes: officialConclusion, latestPrediction | gives: reveal HTML
  let revealSectionHtml = "";
  if (officialConclusion) {
    revealSectionHtml = `
      <div class="reveal-comparison-grid">
        <div class="reveal-card official-conclusion-card">
          <div class="reveal-header">
            <h4>Official Probable Cause / Ground Truth</h4>
            <span class="badge badge-official-finding">Withheld Ground Truth</span>
          </div>
          <div class="reveal-body">
            <p>${escapeHtml(officialConclusion)}</p>
          </div>
          <div class="reveal-footer">Source: NTSB / Official Aviation Investigation Authority</div>
        </div>

        <div class="reveal-card prediction-card">
          <div class="reveal-header">
            <h4>Latest Model Prediction Summary</h4>
            <span class="badge badge-model-hypothesis">Pipeline Assessment</span>
          </div>
          <div class="reveal-body">
            <p>${escapeHtml(latestPrediction || "No prediction run available for comparison.")}</p>
          </div>
          <div class="reveal-footer">Analysis run generated against prediction corpus split</div>
        </div>
      </div>
    `;
  } else {
    revealSectionHtml = `
      <div class="reveal-prompt-card">
        <h4>Withheld Official Ground Truth</h4>
        <p>In accordance with benchmark integrity guards, the official investigation conclusion in <code>data/truth/</code> remains strictly isolated until after at least one prediction run has completed.</p>
        <button type="button" id="btn-reveal-conclusion" class="btn btn-primary btn-reveal">
          Reveal Official Conclusion
        </button>
      </div>
    `;
  }

  // STEP 4: Render complete Evaluation View | takes: runsTableHtml, revealSectionHtml | gives: DOM populated
  containerEl.innerHTML = `
    <div class="panel-section evaluation-panel">
      <div class="evaluation-header">
        <h3>Case Evaluation &amp; Ground Truth Benchmark</h3>
        <p class="evaluation-subtext">Review prior prediction runs and compare model-derived findings with official determinations.</p>
      </div>

      <div class="evaluation-section">
        <h4 class="section-title">Logged Pipeline Runs for Case: ${escapeHtml(caseId || "All")}</h4>
        ${runsTableHtml}
      </div>

      <div class="evaluation-section">
        <h4 class="section-title">Ground Truth Verification Comparison</h4>
        <div id="reveal-container">${revealSectionHtml}</div>
      </div>
    </div>
  `;

  // STEP 5: Attach reveal button click listener if present | takes: onRevealClick | gives: active button
  const revealBtn = containerEl.querySelector("#btn-reveal-conclusion");
  if (revealBtn && typeof onRevealClick === "function") {
    revealBtn.onclick = (e) => {
      e.preventDefault();
      onRevealClick();
    };
  }
}

/**
 * Render self-test visible verification checklist banner (?mock=1).
 *
 * IN: containerEl (HTMLElement), testResults (object).
 * OUT: void.
 * FAILS WITH: ComponentError if containerEl is invalid.
 */
export function renderSelftestVerdict(containerEl, testResults) {
  // STEP 1: Validate containerEl | takes: containerEl | gives: verified element
  if (!containerEl || typeof containerEl.innerHTML === "undefined") {
    throw new ComponentError("C9", "renderSelftestVerdict", "Invalid containerEl provided; must be a valid DOM element");
  }

  // STEP 2: Evaluate core contract checklist items | takes: testResults | gives: checklist booleans
  const {
    schematicPass,
    assessmentPass,
    drawerPass,
    metricsPass,
    presetsPass = true,
    fallbackGuardPass = true,
    failureReasons = []
  } = testResults;

  const allPass = schematicPass && assessmentPass && drawerPass && metricsPass && presetsPass && fallbackGuardPass;

  const itemBadge = (pass) => pass
    ? `<span class="selftest-check pass">&#x2714; PASS</span>`
    : `<span class="selftest-check fail">&#x2718; FAIL</span>`;

  // STEP 3: Populate verdict banner markup | takes: checklist booleans | gives: banner HTML
  containerEl.innerHTML = `
    <div class="selftest-banner ${allPass ? 'pass' : 'fail'}" role="status" aria-label="Self-test verification status">
      <div class="selftest-header">
        <span class="selftest-status-pill ${allPass ? 'status-pass' : 'status-fail'}">
          ${allPass ? 'SELF-TEST PASS' : 'SELF-TEST FAIL'}
        </span>
        <span class="selftest-title">Component C9 Dashboard Shell Self-Test (Mock Mode)</span>
      </div>
      <div class="selftest-checklist">
        <div class="selftest-item">
          ${itemBadge(schematicPass)}
          <span>Schematic rendered (interactive SVG airframe mounted with regions)</span>
        </div>
        <div class="selftest-item">
          ${itemBadge(assessmentPass)}
          <span>Assessment rendered (brief summary, hypotheses, findings, safety note)</span>
        </div>
        <div class="selftest-item">
          ${itemBadge(drawerPass)}
          <span>Citation drawer opens (passage excerpt, highlighted quote, PDF link)</span>
        </div>
        <div class="selftest-item">
          ${itemBadge(metricsPass)}
          <span>Metrics strip shows values (verified measured metrics without fabricated numbers)</span>
        </div>
        <div class="selftest-item">
          ${itemBadge(presetsPass)}
          <span>Benchmark presets &amp; mode options (BUDGET_BY_MODE, question preset, 'live' absent)</span>
        </div>
        <div class="selftest-item">
          ${itemBadge(fallbackGuardPass)}
          <span>Fixture fallback guard (disabled when mockMode is false)</span>
        </div>
      </div>
      ${failureReasons.length > 0 ? `
        <div class="selftest-reasons">
          <strong>Failure Details:</strong>
          <ul>${failureReasons.map(r => `<li>${escapeHtml(r)}</li>`).join("")}</ul>
        </div>
      ` : ""}
    </div>
  `;

  trace("C9", "Self-test verdict banner rendered", { allPass });
}

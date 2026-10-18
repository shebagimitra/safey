// ============================================================
// COMPONENT: C8 - Aircraft Schematic Display
// PURPOSE: Interactive SVG top-down aircraft schematic visualizing component statuses and evidence passage counts.
// INPUT: Container HTMLElement, component IDs ("engine", "landing_gear", "hydraulic_system", "wing_flaps", "fuel_system"), status strings, counts.
// OUTPUT: Rendered inline SVG schematic in containerEl, visual status updates, and "component-selected" CustomEvents.
// CALLED BY: frontend/schematic_demo.html, main application UI      CALLS: DOM APIs, SVG DOM APIs
// IF IT BREAKS, CHECK: SVG element IDs matching component IDs, valid containerEl element, CSS variable inheritance.
// ============================================================

/**
 * Custom error class matching pipeline diagnostic conventions.
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
 * The 5 stable component region IDs specified in blueprint and requirements.
 */
export const VALID_COMPONENTS = Object.freeze([
  "engine",
  "landing_gear",
  "hydraulic_system",
  "wing_flaps",
  "fuel_system"
]);

/**
 * Documented Mapping Table: Evidence Status -> Section 12.4 Status Colors
 * ------------------------------------------------------------------------
 * Color Semantics: Statuses describe EVIDENCE STRENGTH, not severity.
 *
 * Model-Driven Evidence Strength Colors:
 * - Red: Strong documented evidence (open briefing).
 * - Amber: Mixed or contradictory evidence.
 * - Gray: Insufficient evidence or not assessed (default baseline).
 *
 * Reserved Statuses (not model-driven):
 * - Blue: Historical or reference evidence (reserved reference records).
 * - Green: Only verified documented corrective action, never model-inferred or recommended.
 *
 * Rules:
 * - Never imply a component is safe because nothing was retrieved.
 * - Gray is the default baseline for unassessed or insufficient evidence.
 * - Never default a region to green.
 *
 * Mapping Table:
 * - "supported"     -> "red"   (strong documented evidence, open briefing)
 * - "mixed"         -> "amber" (mixed or contradictory evidence)
 * - "contradictory" -> "amber" (mixed or contradictory evidence)
 * - "insufficient"  -> "gray"  (insufficient evidence - default baseline)
 * - "unassessed"    -> "gray"  (not assessed - default baseline)
 * - "historical"    -> "blue"  (historical/reference evidence - reserved, not model-driven)
 * - "corrective"    -> "green" (verified documented corrective action only - reserved, not model-driven)
 */
export const STATUS_TO_COLOR = Object.freeze({
  supported: "red",
  mixed: "amber",
  contradictory: "amber",
  insufficient: "gray",
  unassessed: "gray",
  historical: "blue",
  corrective: "green",
  // Direct color names permitted as pass-through
  red: "red",
  amber: "amber",
  blue: "blue",
  green: "green",
  gray: "gray"
});

// Human-readable component display names
const COMPONENT_LABELS = Object.freeze({
  engine: "Engines",
  landing_gear: "Landing Gear",
  hydraulic_system: "Hydraulic System",
  wing_flaps: "Wing Flaps",
  fuel_system: "Fuel System"
});

// Module-level reference to the mounted container element
let currentContainer = null;
let currentSelectedComponent = null;

/**
 * Get human-readable component display label.
 *
 * IN: componentId (string).
 * OUT: Formatted display label (string).
 * FAILS WITH: Never fails.
 */
function _getHumanLabel(componentId) {
  // STEP 1: Resolve display label from lookup table or fallback to id | takes: componentId | gives: label string
  return COMPONENT_LABELS[componentId] || componentId;
}

/**
 * Get human-readable description for Section 12.4 evidence strength status color.
 *
 * IN: color (string).
 * OUT: Descriptive label (string).
 * FAILS WITH: Never fails.
 */
function _getColorDescription(color) {
  // STEP 1: Map color code to Section 12.4 evidence strength description | takes: color | gives: description string
  switch (color) {
    case "red":
      return "Red: Strong documented evidence (open briefing)";
    case "amber":
      return "Amber: Mixed or contradictory evidence";
    case "gray":
      return "Gray: Insufficient evidence or not assessed";
    case "blue":
      return "Blue: Historical or reference evidence (reserved, not model-driven)";
    case "green":
      return "Green: Verified documented corrective action only (reserved, not model-driven)";
    default:
      return "Gray: Insufficient evidence or not assessed";
  }
}

/**
 * Build inline SVG template for the top-down aircraft schematic.
 * ViewBox "-80 0 960 680" provides wide margins so labels never clip or overlap.
 *
 * IN: None.
 * OUT: Complete SVG HTML markup string.
 * FAILS WITH: Never fails.
 */
function _buildSchematicMarkup() {
  // STEP 1: Construct SVG markup containing airframe, 5 interactive regions, labels, and badges | takes: None | gives: markup string
  return `
<div class="schematic-root" role="region" aria-label="Interactive Aircraft Schematic">
  <div class="schematic-canvas-wrapper">
    <svg class="schematic-svg" viewBox="-80 0 960 680" focusable="false" tabindex="-1" xmlns="http://www.w3.org/2000/svg" aria-label="Top-down schematic view of aircraft systems">
      <defs>
        <!-- Projector-friendly drop-shadow filter for airframe depth -->
        <filter id="schematic-shadow" x="-5%" y="-5%" width="110%" height="110%">
          <feDropShadow dx="0" dy="3" stdDeviation="4" flood-opacity="0.2" />
        </filter>
        <!-- High-visibility selection glow filter -->
        <filter id="selection-glow" x="-15%" y="-15%" width="130%" height="130%">
          <feDropShadow dx="0" dy="0" stdDeviation="6" flood-color="var(--schematic-selected-outline, #2563eb)" flood-opacity="0.8" />
        </filter>
      </defs>

      <!-- BASE AIRFRAME STRUCTURE (Fuselage, Wings, Tail, Cockpit) -->
      <g class="airframe-base" filter="url(#schematic-shadow)">
        <!-- Swept Wings Outline -->
        <path class="airframe-wing" d="M 375 235 L 60 360 L 60 385 L 375 345 Z" />
        <path class="airframe-wing" d="M 425 235 L 740 360 L 740 385 L 425 345 Z" />

        <!-- Horizontal Stabilizers (Tail Wings) -->
        <path class="airframe-tail" d="M 385 495 L 250 560 L 250 580 L 390 550 Z" />
        <path class="airframe-tail" d="M 415 495 L 550 560 L 550 580 L 410 550 Z" />

        <!-- Vertical Stabilizer Fin Outline -->
        <path class="airframe-fin" d="M 397 460 L 403 460 L 403 565 L 397 565 Z" />

        <!-- Fuselage Body Silhouette -->
        <path class="airframe-fuselage" d="
          M 400 45
          C 385 45, 370 75, 370 120
          L 370 470
          C 370 520, 395 565, 400 580
          C 405 565, 430 520, 430 470
          L 430 120
          C 430 75, 415 45, 400 45
          Z" />

        <!-- Cockpit Windshield Canopy -->
        <polygon class="airframe-cockpit" points="390,92 410,92 416,108 384,108" />
        <line class="airframe-cockpit-divider" x1="400" y1="92" x2="400" y2="108" />
        <line class="airframe-centerline" x1="400" y1="120" x2="400" y2="460" />
      </g>

      <!-- REGION 5: FUEL SYSTEM (Center & Wing Fuel Tanks) -->
      <g id="fuel_system" class="schematic-region status-gray" data-component="fuel_system" tabindex="0" role="button" aria-label="Fuel System: unassessed, 0 passages">
        <!-- Center Fuel Tank (fuselage wing box) -->
        <rect class="fuel-tank-surface" x="376" y="246" width="48" height="66" rx="4" />
        <!-- Left Wing Fuel Tank -->
        <polygon class="fuel-tank-surface" points="372,250 200,312 200,332 372,330" />
        <!-- Right Wing Fuel Tank -->
        <polygon class="fuel-tank-surface" points="428,250 600,312 600,332 428,330" />
        <!-- Fuel tank baffles / internal ribs -->
        <line class="fuel-baffle" x1="286" y1="281" x2="286" y2="330" />
        <line class="fuel-baffle" x1="514" y1="281" x2="514" y2="330" />
        <line class="fuel-baffle" x1="376" y1="279" x2="424" y2="279" />
        <!-- Leader line connecting right fuel tank to label pill -->
        <line class="label-leader" x1="580" y1="334" x2="650" y2="426" />
        <!-- Label & Passage Count Badge (Aft Right - Well clear of wing trailing edge and 180px inside right boundary) -->
        <g class="region-label-group" transform="translate(650, 440)">
          <rect class="label-bg" x="-50" y="-12" width="100" height="24" rx="12" />
          <text class="region-label" text-anchor="middle" y="4">Fuel System</text>
          <g class="schematic-badge" data-badge-for="fuel_system" transform="translate(0, 24)">
            <rect class="badge-bg" x="-18" y="-10" width="36" height="20" rx="10" />
            <text class="badge-text" text-anchor="middle" y="4">0</text>
          </g>
        </g>
      </g>

      <!-- REGION 4: WING FLAPS (Trailing Edge Flap Segments) -->
      <g id="wing_flaps" class="schematic-region status-gray" data-component="wing_flaps" tabindex="0" role="button" aria-label="Wing Flaps: unassessed, 0 passages">
        <!-- Left Inboard Flap -->
        <polygon class="flap-surface" points="245,350 365,338 365,358 245,372" />
        <!-- Left Outboard Flap -->
        <polygon class="flap-surface" points="105,366 235,351 235,373 105,386" />
        <!-- Right Inboard Flap -->
        <polygon class="flap-surface" points="435,338 555,350 555,372 435,358" />
        <!-- Right Outboard Flap -->
        <polygon class="flap-surface" points="565,351 695,366 695,386 565,373" />
        <!-- Flap Track Fairings -->
        <rect class="flap-track" x="178" y="360" width="6" height="18" rx="2" />
        <rect class="flap-track" x="298" y="348" width="6" height="18" rx="2" />
        <rect class="flap-track" x="496" y="348" width="6" height="18" rx="2" />
        <rect class="flap-track" x="616" y="360" width="6" height="18" rx="2" />
        <!-- Leader line connecting left flap to label pill -->
        <line class="label-leader" x1="150" y1="388" x2="150" y2="426" />
        <!-- Label & Passage Count Badge (Aft Left - Well clear of wing trailing edge and 180px inside left boundary) -->
        <g class="region-label-group" transform="translate(150, 440)">
          <rect class="label-bg" x="-50" y="-12" width="100" height="24" rx="12" />
          <text class="region-label" text-anchor="middle" y="4">Wing Flaps</text>
          <g class="schematic-badge" data-badge-for="wing_flaps" transform="translate(0, 24)">
            <rect class="badge-bg" x="-18" y="-10" width="36" height="20" rx="10" />
            <text class="badge-text" text-anchor="middle" y="4">0</text>
          </g>
        </g>
      </g>

      <!-- REGION 3: HYDRAULIC SYSTEM (Schematic Line Network along Wings and Fuselage) -->
      <g id="hydraulic_system" class="schematic-region status-gray" data-component="hydraulic_system" tabindex="0" role="button" aria-label="Hydraulic System: unassessed, 0 passages">
        <!-- Transparent wide hit areas for smooth cursor hover and click detection -->
        <path class="hydraulic-hit-path" d="M 397 145 L 397 530 M 403 145 L 403 530 M 397 290 L 180 360 M 403 290 L 620 360 M 341 310 L 341 350 M 459 310 L 459 350" stroke="transparent" stroke-width="26" fill="none" />

        <!-- Dual Central Trunk Lines -->
        <path class="hydraulic-line primary" d="M 397 145 L 397 530" />
        <path class="hydraulic-line secondary" d="M 403 145 L 403 530" />

        <!-- Left Wing Hydraulic Traces -->
        <path class="hydraulic-line primary" d="M 397 290 L 341 310 L 180 360" />
        <path class="hydraulic-line secondary" d="M 341 310 L 341 350" />
        <path class="hydraulic-line secondary" d="M 270 330 L 270 355" />

        <!-- Right Wing Hydraulic Traces -->
        <path class="hydraulic-line primary" d="M 403 290 L 459 310 L 620 360" />
        <path class="hydraulic-line secondary" d="M 459 310 L 459 350" />
        <path class="hydraulic-line secondary" d="M 530 330 L 530 355" />

        <!-- Hydraulic Manifold / Pump Station (Mid Fuselage) -->
        <rect class="hydraulic-manifold" x="389" y="278" width="22" height="24" rx="4" />
        <circle class="hydraulic-pump" cx="396" cy="290" r="4" />
        <circle class="hydraulic-pump" cx="404" cy="290" r="4" />

        <!-- Actuator / Valve Nodes -->
        <circle class="hydraulic-node" cx="397" cy="148" r="4" />
        <circle class="hydraulic-node" cx="341" cy="340" r="4" />
        <circle class="hydraulic-node" cx="459" cy="340" r="4" />
        <circle class="hydraulic-node" cx="180" cy="360" r="4" />
        <circle class="hydraulic-node" cx="620" cy="360" r="4" />
        <circle class="hydraulic-node" cx="400" cy="525" r="4" />

        <!-- Label & Passage Count Badge (Aft Center - Along aft hydraulic trunk, clear of wings and other labels) -->
        <g class="region-label-group" transform="translate(400, 500)">
          <rect class="label-bg" x="-64" y="-12" width="128" height="24" rx="12" />
          <text class="region-label" text-anchor="middle" y="4">Hydraulic System</text>
          <g class="schematic-badge" data-badge-for="hydraulic_system" transform="translate(0, 24)">
            <rect class="badge-bg" x="-18" y="-10" width="36" height="20" rx="10" />
            <text class="badge-text" text-anchor="middle" y="4">0</text>
          </g>
        </g>
      </g>

      <!-- REGION 1: ENGINES (Two Jet Engines on Wings) -->
      <g id="engine" class="schematic-region status-gray" data-component="engine" tabindex="0" role="button" aria-label="Engines: unassessed, 0 passages">
        <!-- Left Engine -->
        <rect class="engine-pylon" x="266" y="248" width="8" height="28" rx="2" />
        <rect class="engine-body" x="246" y="272" width="48" height="80" rx="14" />
        <ellipse class="engine-intake" cx="270" cy="274" rx="22" ry="7" />
        <circle class="engine-spinner" cx="270" cy="276" r="5" />
        <path class="engine-exhaust" d="M 254 352 L 270 366 L 286 352 Z" />

        <!-- Right Engine -->
        <rect class="engine-pylon" x="526" y="248" width="8" height="28" rx="2" />
        <rect class="engine-body" x="506" y="272" width="48" height="80" rx="14" />
        <ellipse class="engine-intake" cx="530" cy="274" rx="22" ry="7" />
        <circle class="engine-spinner" cx="530" cy="276" r="5" />
        <path class="engine-exhaust" d="M 514 352 L 530 366 L 546 352 Z" />

        <!-- Leader line from left engine intake to forward label -->
        <line class="label-leader" x1="270" y1="236" x2="270" y2="246" />

        <!-- Label & Passage Count Badge (Forward Left - In clear air ahead of left engine, NO wing overlap) -->
        <g class="region-label-group" transform="translate(270, 200)">
          <rect class="label-bg" x="-46" y="-12" width="92" height="24" rx="12" />
          <text class="region-label" text-anchor="middle" y="4">Engines</text>
          <g class="schematic-badge" data-badge-for="engine" transform="translate(0, 24)">
            <rect class="badge-bg" x="-18" y="-10" width="36" height="20" rx="10" />
            <text class="badge-text" text-anchor="middle" y="4">0</text>
          </g>
        </g>
      </g>

      <!-- REGION 2: LANDING GEAR (Nose Gear & Left/Right Main Gear) -->
      <g id="landing_gear" class="schematic-region status-gray" data-component="landing_gear" tabindex="0" role="button" aria-label="Landing Gear: unassessed, 0 passages">
        <!-- Nose Landing Gear -->
        <rect class="gear-bay" x="390" y="125" width="20" height="32" rx="4" />
        <line class="gear-strut" x1="400" y1="128" x2="400" y2="152" />
        <line class="gear-axle" x1="389" y1="143" x2="411" y2="143" />
        <rect class="gear-tire" x="386" y="136" width="6" height="14" rx="2" />
        <rect class="gear-tire" x="408" y="136" width="6" height="14" rx="2" />

        <!-- Left Main Landing Gear -->
        <rect class="gear-bay" x="325" y="322" width="32" height="36" rx="4" />
        <line class="gear-strut" x1="341" y1="324" x2="341" y2="354" />
        <rect class="gear-tire" x="327" y="326" width="6" height="12" rx="2" />
        <rect class="gear-tire" x="349" y="326" width="6" height="12" rx="2" />
        <rect class="gear-tire" x="327" y="342" width="6" height="12" rx="2" />
        <rect class="gear-tire" x="349" y="342" width="6" height="12" rx="2" />

        <!-- Right Main Landing Gear -->
        <rect class="gear-bay" x="443" y="322" width="32" height="36" rx="4" />
        <line class="gear-strut" x1="459" y1="324" x2="459" y2="354" />
        <rect class="gear-tire" x="445" y="326" width="6" height="12" rx="2" />
        <rect class="gear-tire" x="467" y="326" width="6" height="12" rx="2" />
        <rect class="gear-tire" x="445" y="342" width="6" height="12" rx="2" />
        <rect class="gear-tire" x="467" y="342" width="6" height="12" rx="2" />

        <!-- Label & Passage Count Badge (Forward Center - Below nose gear, above wing box) -->
        <g class="region-label-group" transform="translate(400, 175)">
          <rect class="label-bg" x="-54" y="-12" width="108" height="24" rx="12" />
          <text class="region-label" text-anchor="middle" y="4">Landing Gear</text>
          <g class="schematic-badge" data-badge-for="landing_gear" transform="translate(0, 24)">
            <rect class="badge-bg" x="-18" y="-10" width="36" height="20" rx="10" />
            <text class="badge-text" text-anchor="middle" y="4">0</text>
          </g>
        </g>
      </g>
    </svg>

    <!-- Floating Hover Tooltip -->
    <div class="schematic-tooltip" role="tooltip" aria-hidden="true"></div>
  </div>

  <!-- EVIDENCE STRENGTH LEGEND (Blueprint §12.4 Semantics) -->
  <aside class="schematic-legend" aria-label="Evidence Strength Legend">
    <div class="legend-header">
      <span class="legend-title">Evidence Strength Legend</span>
      <span class="legend-subtitle">(Blueprint §12.4 Semantics)</span>
    </div>
    <ul class="legend-items">
      <li class="legend-item">
        <span class="legend-swatch status-red"></span>
        <span class="legend-text"><strong>Red:</strong> Strong documented evidence (open briefing)</span>
      </li>
      <li class="legend-item">
        <span class="legend-swatch status-amber"></span>
        <span class="legend-text"><strong>Amber:</strong> Mixed or contradictory evidence</span>
      </li>
      <li class="legend-item">
        <span class="legend-swatch status-gray"></span>
        <span class="legend-text"><strong>Gray:</strong> Insufficient evidence or not assessed (default baseline)</span>
      </li>
    </ul>
    <div class="legend-reserved-note">
      <strong>Reserved (not model-driven):</strong>
      <span class="legend-swatch-inline status-blue"></span> <strong>Blue:</strong> Historical or reference evidence &bull;
      <span class="legend-swatch-inline status-green"></span> <strong>Green:</strong> Verified documented corrective action only. Never model-inferred or defaulted.
    </div>
    <div class="legend-disclaimer">
      <strong>Important:</strong> Never default a region to green. Gray denotes insufficient evidence or unassessed status, never an implication of safety.
    </div>
  </aside>
</div>
`;
}

/**
 * Configure hover tooltip tracking and keyboard focus announcement.
 *
 * IN: containerEl (HTMLElement).
 * OUT: void.
 * FAILS WITH: ComponentError if tooltip element is missing.
 */
function _setupTooltip(containerEl) {
  // STEP 1: Query tooltip element in container | takes: containerEl | gives: tooltipEl
  const tooltipEl = containerEl.querySelector(".schematic-tooltip");
  if (!tooltipEl) {
    throw new ComponentError("C8", "setupTooltip", "Missing .schematic-tooltip in DOM");
  }

  // STEP 2: Bind mouse and keyboard listeners across all interactive regions | takes: containerEl, tooltipEl | gives: bound handlers
  const regions = containerEl.querySelectorAll(".schematic-region");
  regions.forEach((region) => {
    const compId = region.dataset.component;

    // Show tooltip on hover
    region.addEventListener("mouseenter", () => {
      const label = _getHumanLabel(compId);
      const status = region.dataset.status || "unassessed";
      const color = region.dataset.color || "gray";
      const count = region.dataset.count || "0";
      const desc = _getColorDescription(color);

      tooltipEl.innerHTML = `
        <div class="tooltip-title">${label}</div>
        <div class="tooltip-status"><span class="tooltip-dot ${color}"></span> ${desc}</div>
        <div class="tooltip-count">Passages: <strong>${count}</strong></div>
      `;
      tooltipEl.classList.add("visible");
      tooltipEl.setAttribute("aria-hidden", "false");
    });

    // Move tooltip with cursor
    region.addEventListener("mousemove", (e) => {
      const rect = containerEl.getBoundingClientRect();
      const x = e.clientX - rect.left + 14;
      const y = e.clientY - rect.top + 14;
      tooltipEl.style.left = `${x}px`;
      tooltipEl.style.top = `${y}px`;
    });

    // Hide tooltip on mouse leave
    region.addEventListener("mouseleave", () => {
      tooltipEl.classList.remove("visible");
      tooltipEl.setAttribute("aria-hidden", "true");
    });

    // Accessible keyboard focus support
    region.addEventListener("focusin", () => {
      const label = _getHumanLabel(compId);
      const status = region.dataset.status || "unassessed";
      const color = region.dataset.color || "gray";
      const count = region.dataset.count || "0";
      const desc = _getColorDescription(color);

      tooltipEl.innerHTML = `
        <div class="tooltip-title">${label}</div>
        <div class="tooltip-status"><span class="tooltip-dot ${color}"></span> ${desc}</div>
        <div class="tooltip-count">Passages: <strong>${count}</strong></div>
      `;
      const regionRect = region.getBoundingClientRect();
      const containerRect = containerEl.getBoundingClientRect();
      const x = regionRect.left - containerRect.left + regionRect.width / 2;
      const y = regionRect.top - containerRect.top;
      tooltipEl.style.left = `${x}px`;
      tooltipEl.style.top = `${y}px`;
      tooltipEl.classList.add("visible");
      tooltipEl.setAttribute("aria-hidden", "false");
    });

    region.addEventListener("focusout", () => {
      tooltipEl.classList.remove("visible");
      tooltipEl.setAttribute("aria-hidden", "true");
    });
  });
}

/**
 * Handle user selection click or keyboard activation.
 *
 * IN: componentId (string), containerEl (HTMLElement).
 * OUT: void.
 * FAILS WITH: ComponentError on event dispatch failure.
 */
function _handleRegionClick(componentId, containerEl) {
  // STEP 1: Update visual selection state | takes: componentId | gives: updated selection
  setSelected(componentId);

  // STEP 2: Dispatch CustomEvent "component-selected" on containerEl | takes: componentId, containerEl | gives: dispatched event
  const selectEvent = new CustomEvent("component-selected", {
    detail: { componentId },
    bubbles: true,
    composed: true
  });
  containerEl.dispatchEvent(selectEvent);

  // STEP 3: Emit structured telemetry trace | takes: componentId | gives: void
  trace("C8", "Dispatched component-selected event", { componentId });
}

/**
 * Mount the aircraft schematic into a host container element.
 *
 * IN: containerEl (HTMLElement).
 * OUT: void.
 * FAILS WITH: ComponentError if containerEl is missing or not a valid DOM Element.
 */
export function mountSchematic(containerEl) {
  // STEP 1: Validate containerEl input | takes: containerEl | gives: validated container
  if (!containerEl || typeof containerEl.appendChild !== "function") {
    throw new ComponentError("C8", "mountSchematic", "Invalid containerEl provided; must be a valid DOM Element");
  }

  // STEP 2: Store current container reference | takes: containerEl | gives: module state updated
  currentContainer = containerEl;
  currentSelectedComponent = null;

  // STEP 3: Render schematic markup into container | takes: containerEl | gives: populated DOM
  containerEl.innerHTML = _buildSchematicMarkup();

  // STEP 4: Setup hover tooltips and keyboard accessibility | takes: containerEl | gives: initialized tooltips
  _setupTooltip(containerEl);

  // STEP 5: Attach click and keyboard event listeners to the 5 regions | takes: containerEl | gives: active event handlers
  VALID_COMPONENTS.forEach((componentId) => {
    const regionEl = containerEl.querySelector(`#${componentId}`);
    if (!regionEl) {
      throw new ComponentError("C8", "mountSchematic", `Failed locating region element #${componentId}`);
    }

    // Click activation
    regionEl.addEventListener("click", (e) => {
      e.preventDefault();
      _handleRegionClick(componentId, containerEl);
    });

    // Keyboard activation (Enter or Space key)
    regionEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        _handleRegionClick(componentId, containerEl);
      }
    });

    // Initialize to gray unassessed baseline (never green by default)
    setRegionStatus(componentId, "unassessed", 0);
  });

  // STEP 6: Emit mount completion trace | takes: None | gives: void
  trace("C8", "Mounted aircraft schematic", {
    containerTag: containerEl.tagName,
    regionCount: VALID_COMPONENTS.length
  });
}

/**
 * Set the evidence status and passage count for an aircraft component region.
 *
 * IN: componentId (string), status (string), count (number).
 * OUT: void.
 * FAILS WITH: ComponentError if componentId is invalid, status is unknown, or DOM region missing.
 */
export function setRegionStatus(componentId, status, count = 0) {
  // STEP 1: Validate componentId is one of the 5 controlled component IDs | takes: componentId | gives: validated componentId
  if (!VALID_COMPONENTS.includes(componentId)) {
    throw new ComponentError("C8", "setRegionStatus", `Invalid componentId '${componentId}'. Must be one of: ${VALID_COMPONENTS.join(", ")}`);
  }

  // STEP 2: Map evidence status to Section 12.4 color via documented mapping table | takes: status | gives: targetColor
  const normalizedStatus = (status || "unassessed").toLowerCase().trim();
  const targetColor = STATUS_TO_COLOR[normalizedStatus];
  if (!targetColor) {
    throw new ComponentError("C8", "setRegionStatus", `Unrecognized status '${status}'. Must map to supported, mixed, insufficient, contradictory, or unassessed`);
  }

  // STEP 3: Locate target region and badge elements | takes: componentId | gives: regionEl, badgeTextEl
  const targetScope = currentContainer || document;
  const regionEl = targetScope.querySelector(`#${componentId}`);
  if (!regionEl) {
    throw new ComponentError("C8", "setRegionStatus", `Region element #${componentId} not found in schematic DOM`);
  }

  const badgeTextEl = regionEl.querySelector(`[data-badge-for="${componentId}"] .badge-text`) ||
                      regionEl.querySelector(".badge-text");

  // STEP 4: Update CSS status color classes and data attributes | takes: regionEl, targetColor | gives: updated classes
  const statusClasses = ["status-red", "status-amber", "status-blue", "status-green", "status-gray"];
  statusClasses.forEach((cls) => regionEl.classList.remove(cls));
  regionEl.classList.add(`status-${targetColor}`);

  regionEl.dataset.status = normalizedStatus;
  regionEl.dataset.color = targetColor;
  regionEl.dataset.count = String(count);

  // STEP 5: Update passage count text badge and accessibility label | takes: badgeTextEl, count | gives: updated badge
  if (badgeTextEl) {
    badgeTextEl.textContent = String(count);
  }
  const humanLabel = _getHumanLabel(componentId);
  regionEl.setAttribute("aria-label", `${humanLabel}: ${normalizedStatus} (${targetColor}), ${count} passage${count === 1 ? "" : "s"}`);

  // STEP 6: Emit status update trace | takes: componentId, normalizedStatus, count, targetColor | gives: void
  trace("C8", "Updated region status", {
    componentId,
    status: normalizedStatus,
    count,
    color: targetColor
  });
}

/**
 * Set the actively selected component region and update its visual outline.
 *
 * IN: componentId (string or null).
 * OUT: void.
 * FAILS WITH: ComponentError if componentId is invalid.
 */
export function setSelected(componentId) {
  // STEP 1: Validate componentId if non-null | takes: componentId | gives: validated componentId
  if (componentId !== null && componentId !== undefined && !VALID_COMPONENTS.includes(componentId)) {
    throw new ComponentError("C8", "setSelected", `Invalid componentId '${componentId}' for selection`);
  }

  // STEP 2: Clear active selection from all regions | takes: currentContainer | gives: unselected regions
  const targetScope = currentContainer || document;
  const regions = targetScope.querySelectorAll(".schematic-region");
  regions.forEach((reg) => {
    reg.classList.remove("is-selected");
    reg.setAttribute("aria-pressed", "false");
  });

  // STEP 3: Apply selection styling to target region if specified | takes: componentId | gives: selected region
  currentSelectedComponent = componentId || null;
  if (componentId) {
    const targetEl = targetScope.querySelector(`#${componentId}`);
    if (targetEl) {
      targetEl.classList.add("is-selected");
      targetEl.setAttribute("aria-pressed", "true");
    }
  }

  // STEP 4: Emit selection trace | takes: componentId | gives: void
  trace("C8", "Selected component", { selected: componentId || "none" });
}

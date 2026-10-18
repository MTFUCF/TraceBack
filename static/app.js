"use strict";

const HOP_DEFINITIONS = [
  { id: "H1", key: "original_user", name: "Original human requester" },
  { id: "H2", key: "copilot_agent", name: "Copilot or originating agent" },
  { id: "H3", key: "foundry_agent", name: "Foundry agent" },
  { id: "H4", key: "mcp_tool", name: "MCP-hosting service" },
  { id: "H5", key: "workload_identity", name: "Workload identity or token subject" },
  { id: "H6", key: "api_action", name: "API action" },
  { id: "H7", key: "target", name: "Affected target" },
];

const MODE_LABELS = {
  delegated_permissions: "Delegated permissions",
  application_permissions: "Application permissions",
};

const TIMESPAN_LABELS = {
  P7D: "Previous 7 days",
  P30D: "Previous 30 days",
  P90D: "Previous 90 days",
};

const STATE_COPY = {
  complete: {
    label: "Complete",
    title: "Evidence chain complete",
    detail: "All seven hops and six adjacent links were reported as supported.",
    tone: "success",
  },
  partial: {
    label: "Partial",
    title: "Partial evidence returned",
    detail: "Some evidence or source coverage is missing. Treat supported links as incomplete context.",
    tone: "warning",
  },
  "no-evidence": {
    label: "No evidence",
    title: "No matching evidence observed",
    detail: "Queried sources returned no evidence for this scope. This does not prove the activity did not occur.",
    tone: "neutral",
  },
  unresolved: {
    label: "Unresolved",
    title: "Attribution remains unresolved",
    detail: "Evidence was observed, but it does not support a coherent end-to-end attribution.",
    tone: "warning",
  },
  impossible: {
    label: "Impossible",
    title: "Attribution is structurally impossible",
    detail: "The available telemetry cannot create the required backward link to a human requester.",
    tone: "danger",
  },
  "telemetry-unavailable": {
    label: "Telemetry unavailable",
    title: "Required telemetry is unavailable",
    detail: "The service could not query enough required sources to assess the chain.",
    tone: "danger",
  },
  "service-failure": {
    label: "Service failure",
    title: "Investigation service failed",
    detail: "The local service did not return a usable investigation response.",
    tone: "danger",
  },
  validation: {
    label: "Input needed",
    title: "Enter a valid target",
    detail: "The requested target must contain at least one visible character.",
    tone: "danger",
  },
  loading: {
    label: "Running",
    title: "Querying telemetry",
    detail: "The investigation is running. Source availability may affect the result.",
    tone: "progress",
  },
  idle: {
    label: "Not started",
    title: "Ready for an investigation",
    detail: "Submit a target to review the seven expected hops, their six links, and any telemetry gaps.",
    tone: "idle",
  },
};

const form = document.getElementById("investigate-form");
const targetInput = document.getElementById("target");
const targetError = document.getElementById("target-error");
const modeInput = document.getElementById("mode");
const timespanInput = document.getElementById("timespan");
const submitButton = document.getElementById("submit-button");
const resultsRegion = document.getElementById("investigation-results");
const resultsHeading = document.getElementById("results-heading");
const resultState = document.getElementById("result-state");
const statePanel = document.getElementById("state-panel");
const stateTitle = document.getElementById("state-title");
const stateDetail = document.getElementById("state-detail");
const resultContent = document.getElementById("result-content");
const announcement = document.getElementById("announcement");
const serviceStatus = document.getElementById("service-status");

let activeRequest = null;
let requestSequence = 0;

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function scalarArray(value) {
  const values = Array.isArray(value) ? value : value === undefined || value === null ? [] : [value];
  return values
    .map((item) => scalarText(item, ""))
    .filter(Boolean);
}

function scalarText(value, fallback = "Not reported") {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return fallback;
}

function normalizeToken(value, fallback = "unknown") {
  if (typeof value !== "string" || !value.trim()) {
    return fallback;
  }
  return value.trim().toLowerCase().replace(/[\s_]+/g, "-");
}

function humanize(value, fallback = "Not reported") {
  if (typeof value !== "string" || !value.trim()) {
    return fallback;
  }
  const words = value.trim().replace(/[_-]+/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function clearNode(node) {
  node.textContent = "";
}

function makeElement(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

function makeDefinition(term, value, className) {
  const wrapper = makeElement("div", className || "definition");
  wrapper.append(
    makeElement("dt", "definition-term", term),
    makeElement("dd", "definition-value", value),
  );
  return wrapper;
}

function statusClass(status) {
  const normalized = normalizeToken(status);
  const supported = new Set([
    "complete",
    "linked",
    "observed",
    "succeeded",
    "ready",
    "partial",
    "candidate",
    "unresolved",
    "unavailable",
    "failed",
    "impossible",
    "no-evidence",
    "unknown",
  ]);
  return supported.has(normalized) ? normalized : "unknown";
}

function confidenceClass(confidence) {
  const normalized = normalizeToken(confidence, "none");
  return ["high", "medium", "low", "none"].includes(normalized) ? normalized : "none";
}

function makeStatusBadge(status) {
  const value = normalizeToken(status);
  return makeElement(
    "span",
    `badge badge-status status-${statusClass(value)}`,
    `Status: ${humanize(value)}`,
  );
}

function makeConfidenceBadge(confidence) {
  const value = normalizeToken(confidence, "none");
  return makeElement(
    "span",
    `badge badge-confidence confidence-${confidenceClass(value)}`,
    `Confidence: ${humanize(value)}`,
  );
}

function makeJoinBadge(joinType) {
  const value = normalizeToken(joinType, "unresolved");
  const labels = {
    "exact-identifier": "Exact ID join",
    "heuristic-time-window": "Heuristic time-window join",
    "no-evidence": "No evidence",
    unresolved: "Unresolved join",
  };
  return makeElement(
    "span",
    `badge badge-join join-${["exact-identifier", "heuristic-time-window", "no-evidence", "unresolved"].includes(value) ? value : "unresolved"}`,
    labels[value] || labels.unresolved,
  );
}

function setBusy(isBusy) {
  resultsRegion.setAttribute("aria-busy", String(isBusy));
  submitButton.disabled = isBusy;
  submitButton.classList.toggle("is-loading", isBusy);
  targetInput.disabled = isBusy;
  modeInput.disabled = isBusy;
  timespanInput.disabled = isBusy;
}

function setState(kind, overrides = {}) {
  const state = STATE_COPY[kind] || STATE_COPY["service-failure"];
  const title = overrides.title || state.title;
  const detail = overrides.detail || state.detail;
  const label = overrides.label || state.label;

  resultState.className = `state-pill state-${state.tone}`;
  resultState.textContent = label;
  statePanel.className = `state-card state-${state.tone}`;
  stateTitle.textContent = title;
  stateDetail.textContent = detail;
  announcement.textContent = `${label}. ${title}. ${detail}`;
}

function focusResults() {
  window.requestAnimationFrame(() => {
    resultsHeading.focus();
  });
}

function showValidation(message) {
  targetInput.setAttribute("aria-invalid", "true");
  targetError.textContent = message;
  targetError.hidden = false;
  setState("validation", { detail: message });
  clearNode(resultContent);
  targetInput.focus();
}

function clearValidation() {
  targetInput.removeAttribute("aria-invalid");
  targetError.textContent = "";
  targetError.hidden = true;
}

function normalizedResponseStatus(data) {
  const status = normalizeToken(data.status);
  const aliases = {
    "no-evidence": "no-evidence",
    unavailable: "telemetry-unavailable",
    "telemetry-unavailable": "telemetry-unavailable",
    "service-failure": "service-failure",
    error: "service-failure",
    failed: "service-failure",
  };
  return aliases[status] || status;
}

function responseHasInvestigationData(data) {
  if (!isRecord(data)) {
    return false;
  }
  return [
    "status",
    "hops",
    "links",
    "sources",
    "query_outcomes",
    "notices",
    "reason_codes",
  ].some((key) => Object.prototype.hasOwnProperty.call(data, key));
}

function stateForResponse(data) {
  const status = normalizedResponseStatus(data);
  const allowed = new Set([
    "complete",
    "partial",
    "no-evidence",
    "unresolved",
    "impossible",
    "telemetry-unavailable",
    "service-failure",
  ]);
  if (!allowed.has(status)) {
    return "partial";
  }

  const hops = normalizeHops(data.hops);
  const links = normalizeLinks(data.links);
  const hasSources = Array.isArray(data.sources) || isRecord(data.sources)
    || Array.isArray(data.query_outcomes);
  const hasContractMetadata = [
    "schema_version",
    "investigation_id",
    "requested_target",
    "window",
  ].every((key) => Object.prototype.hasOwnProperty.call(data, key));
  if (
    status === "complete"
    && (
      hops.filter(Boolean).length !== 7
      || links.filter(Boolean).length !== 6
      || !hasSources
      || !hasContractMetadata
    )
  ) {
    return "partial";
  }
  return status;
}

function normalizeHops(value) {
  const positioned = new Array(HOP_DEFINITIONS.length).fill(null);
  const supplied = Array.isArray(value)
    ? value
    : isRecord(value)
      ? Object.entries(value).map(([key, hop]) => (
        isRecord(hop) ? { ...hop, key: hop.key || key } : { key, value: hop }
      ))
      : [];

  supplied.forEach((hop, suppliedIndex) => {
    if (!isRecord(hop)) {
      return;
    }
    const id = scalarText(hop.id, "").toUpperCase();
    const key = scalarText(hop.key, "");
    const index = HOP_DEFINITIONS.findIndex((definition) => (
      definition.id === id || definition.key === key
    ));
    const targetIndex = index >= 0 ? index : suppliedIndex;
    if (targetIndex >= 0 && targetIndex < positioned.length && !positioned[targetIndex]) {
      positioned[targetIndex] = hop;
    }
  });
  return positioned;
}

function normalizeLinks(value) {
  const positioned = new Array(HOP_DEFINITIONS.length - 1).fill(null);
  asArray(value).forEach((link, suppliedIndex) => {
    if (!isRecord(link)) {
      return;
    }
    const source = scalarText(link.source || link.from, "").toUpperCase();
    const target = scalarText(link.target || link.to, "").toUpperCase();
    const index = HOP_DEFINITIONS.findIndex((definition, definitionIndex) => (
      definitionIndex < HOP_DEFINITIONS.length - 1
      && definition.id === source
      && HOP_DEFINITIONS[definitionIndex + 1].id === target
    ));
    const targetIndex = index >= 0 ? index : suppliedIndex;
    if (targetIndex >= 0 && targetIndex < positioned.length && !positioned[targetIndex]) {
      positioned[targetIndex] = link;
    }
  });
  return positioned;
}

function appendReasoning(parent, item) {
  const explanation = scalarText(item.explanation || item.rationale, "");
  const reasonCodes = asArray(item.reason_codes || item.reasons)
    .filter((reason) => typeof reason === "string" && reason.trim());

  if (!explanation && reasonCodes.length === 0) {
    return;
  }

  const section = makeElement("div", "rationale");
  section.append(makeElement("h5", "detail-heading", "Rationale"));
  if (explanation) {
    section.append(makeElement("p", null, explanation));
  }
  if (reasonCodes.length) {
    const list = makeElement("ul", "token-list");
    reasonCodes.forEach((reason) => {
      list.append(makeElement("li", null, humanize(reason)));
    });
    section.append(list);
  }
  parent.append(section);
}

function typedIdentifiers(item) {
  const identifiers = [];
  const seen = new Set();

  function addIdentifier(type, value) {
    const cleanType = scalarText(type, "");
    const cleanValue = scalarText(value, "");
    if (!cleanType || !cleanValue) {
      return;
    }
    const fingerprint = `${cleanType}\u0000${cleanValue}`;
    if (!seen.has(fingerprint)) {
      seen.add(fingerprint);
      identifiers.push({ type: cleanType, value: cleanValue });
    }
  }

  asArray(item.typed_ids).forEach((entry) => {
    if (isRecord(entry)) {
      const type = entry.type || entry.kind || entry.id_type;
      const values = Array.isArray(entry.values)
        ? entry.values
        : [entry.value || entry.id];
      values.forEach((value) => addIdentifier(type, value));
    }
  });

  [item.typed_ids, item.ids, item.identifiers].forEach((identifierMap) => {
    if (!isRecord(identifierMap)) {
      return;
    }
    Object.entries(identifierMap).forEach(([type, values]) => {
      const entries = Array.isArray(values) ? values : [values];
      entries.forEach((value) => addIdentifier(type, value));
    });
  });

  if (item.correlation_key_type && item.correlation_ref) {
    addIdentifier(item.correlation_key_type, item.correlation_ref);
  }
  if (item.id_type && item.id_value) {
    addIdentifier(item.id_type, item.id_value);
  }
  return identifiers;
}

function renderTypedIdentifiers(parent, item) {
  const identifiers = typedIdentifiers(item);
  if (!identifiers.length) {
    return;
  }

  const section = makeElement("div", "typed-identifiers");
  section.append(makeElement("h6", "detail-heading", "Typed identifiers"));
  const list = makeElement("dl", "identifier-list");
  identifiers.forEach((identifier) => {
    list.append(makeDefinition(humanize(identifier.type), identifier.value, "identifier"));
  });
  section.append(list);
  parent.append(section);
}

function evidenceReference(item) {
  return scalarText(item.evidence_ref || item.reference || item.ref || item.id, "");
}

function renderEvidenceItem(evidence, index) {
  const item = isRecord(evidence) ? evidence : { evidence_ref: evidence };
  const card = makeElement("article", "evidence-card");
  const heading = makeElement("div", "evidence-heading");
  const source = scalarText(item.source || item.table, "Source not reported");
  heading.append(
    makeElement("h6", null, source),
    makeElement("span", "evidence-number", `Record ${index + 1}`),
  );
  card.append(heading);

  const metadata = makeElement("dl", "evidence-metadata");
  metadata.append(
    makeDefinition(
      "Observed time",
      scalarText(item.event_time || item.timestamp || item.time, "Unknown"),
    ),
    makeDefinition(
      "Evidence type",
      humanize(item.evidence_type || item.type, "Not reported"),
    ),
  );
  const reference = evidenceReference(item);
  if (reference) {
    metadata.append(makeDefinition("Evidence reference", reference));
  }
  if (item.outcome || item.status) {
    metadata.append(makeDefinition("Record status", humanize(item.outcome || item.status)));
  }
  card.append(metadata);
  renderTypedIdentifiers(card, item);
  appendReasoning(card, item);
  return card;
}

function renderEvidence(parent, hop) {
  const evidence = asArray(hop.evidence || hop.evidence_records);
  const section = makeElement("div", "evidence-section");
  section.append(makeElement("h5", "detail-heading", "Observed evidence"));

  if (!evidence.length) {
    section.append(
      makeElement(
        "p",
        "empty-detail",
        "No evidence records were supplied for this hop.",
      ),
    );
    parent.append(section);
    return;
  }

  const disclosure = makeElement("details", "evidence-disclosure");
  disclosure.open = true;
  disclosure.append(
    makeElement(
      "summary",
      null,
      `${evidence.length} evidence ${evidence.length === 1 ? "record" : "records"}`,
    ),
  );
  const list = makeElement("div", "evidence-list");
  evidence.forEach((item, index) => {
    list.append(renderEvidenceItem(item, index));
  });
  disclosure.append(list);
  section.append(disclosure);
  parent.append(section);
}

function renderHop(hop, definition, index) {
  const supplied = isRecord(hop);
  const value = supplied ? hop : {};
  const status = supplied ? value.status : "unavailable";
  const confidence = supplied ? value.confidence : "none";
  const card = makeElement("article", "hop-card");
  card.setAttribute("aria-labelledby", `hop-heading-${index + 1}`);

  const header = makeElement("header", "chain-card-header");
  const titleGroup = makeElement("div", "chain-title-group");
  titleGroup.append(
    makeElement("span", "hop-index", definition.id),
    makeElement(
      "h4",
      null,
      scalarText(value.name || value.label, definition.name),
    ),
  );
  titleGroup.lastElementChild.id = `hop-heading-${index + 1}`;

  const badges = makeElement("div", "badge-row");
  badges.append(makeStatusBadge(status), makeConfidenceBadge(confidence));
  header.append(titleGroup, badges);
  card.append(header);

  const observed = makeElement("div", "observed-value");
  observed.append(
    makeElement("span", "observed-label", "Observed value"),
    makeElement(
      "p",
      null,
      scalarText(value.value || value.observed_value, "No value reported"),
    ),
  );
  card.append(observed);

  const evidenceRefs = scalarArray(value.evidence_refs);
  const timestamps = scalarArray(value.timestamps || value.observed_times);
  if (evidenceRefs.length || timestamps.length) {
    const metadata = makeElement("dl", "hop-metadata");
    if (evidenceRefs.length) {
      metadata.append(makeDefinition("Evidence references", evidenceRefs.join(", ")));
    }
    if (timestamps.length) {
      metadata.append(makeDefinition("Observed times", timestamps.join(", ")));
    }
    card.append(metadata);
  }

  renderTypedIdentifiers(card, value);
  renderEvidence(card, value);
  appendReasoning(card, value);
  return card;
}

function renderLink(link, index) {
  const supplied = isRecord(link);
  const value = supplied ? link : {};
  const sourceDefinition = HOP_DEFINITIONS[index];
  const targetDefinition = HOP_DEFINITIONS[index + 1];
  const status = supplied ? value.status : "unavailable";
  const confidence = supplied ? value.confidence : "none";
  const card = makeElement("article", "link-card");
  card.setAttribute(
    "aria-label",
    `Link ${sourceDefinition.id} to ${targetDefinition.id}`,
  );

  const rail = makeElement("div", "link-rail");
  rail.setAttribute("aria-hidden", "true");
  rail.append(makeElement("span", "link-dot"), makeElement("span", "link-line"));

  const body = makeElement("div", "link-body");
  const joinType = normalizeToken(value.join_type, supplied && status === "unavailable" ? "no-evidence" : "unresolved");
  body.classList.add(`link-join-${["exact-identifier", "heuristic-time-window", "no-evidence", "unresolved"].includes(joinType) ? joinType : "unresolved"}`);
  body.append(
    makeElement(
      "h4",
      null,
      `${sourceDefinition.id} to ${targetDefinition.id}: ${sourceDefinition.name} to ${targetDefinition.name}`,
    ),
  );
  const badges = makeElement("div", "badge-row");
  badges.append(makeStatusBadge(status), makeConfidenceBadge(confidence), makeJoinBadge(joinType));
  body.append(badges);

  const details = makeElement("dl", "link-metadata");
  const evidenceRefs = scalarArray(value.evidence_refs);
  const keyTypes = scalarArray(value.key_types || value.identifier_types);
  const timestamps = scalarArray(value.timestamps || value.observed_times);

  details.append(
    makeDefinition(
      "Evidence references",
      evidenceRefs.length ? evidenceRefs.join(", ") : "None reported",
    ),
    makeDefinition(
      "Typed link keys",
      keyTypes.length ? keyTypes.map((key) => humanize(key)).join(", ") : "None reported",
    ),
  );

  if (typeof value.time_delta_seconds === "number") {
    details.append(
      makeDefinition("Observed time delta", `${value.time_delta_seconds} seconds`),
    );
  }
  if (typeof value.time_window_seconds === "number") {
    details.append(
      makeDefinition("Allowed link window", `${value.time_window_seconds} seconds`),
    );
  }
  if (timestamps.length) {
    details.append(makeDefinition("Observed times", timestamps.join(", ")));
  }
  body.append(details);
  if (joinType === "heuristic-time-window") {
    body.append(
      makeElement(
        "p",
        "heuristic-caveat",
        "Heuristic only: another user or request using the same agent, tool, or identity inside this window could be falsely attributed.",
      ),
    );
  }
  renderTypedIdentifiers(body, value);
  appendReasoning(body, value);
  card.append(rail, body);
  return card;
}

function renderRequestedScope(data, request) {
  const section = makeElement("section", "requested-scope");
  section.setAttribute("aria-labelledby", "requested-scope-heading");
  section.append(
    makeElement("p", "scope-kicker", "Requested scope — operator input"),
    makeElement("h3", null, scalarText(data.requested_target, request.target)),
  );
  section.lastElementChild.id = "requested-scope-heading";

  const metadata = makeElement("dl", "scope-metadata");
  const windowValue = isRecord(data.window)
    ? data.window.label || data.window.timespan || data.window.duration
    : data.window;
  metadata.append(
    makeDefinition("Permission model", MODE_LABELS[data.mode] || humanize(data.mode || request.mode)),
    makeDefinition(
      "Telemetry window",
      scalarText(windowValue, TIMESPAN_LABELS[request.timespan] || request.timespan),
    ),
  );
  if (isRecord(data.window)) {
    if (data.window.start || data.window.start_time) {
      metadata.append(
        makeDefinition("Window start", scalarText(data.window.start || data.window.start_time)),
      );
    }
    if (data.window.end || data.window.end_time) {
      metadata.append(
        makeDefinition("Window end", scalarText(data.window.end || data.window.end_time)),
      );
    }
  }
  section.append(metadata);
  return section;
}

function renderInvestigationSummary(data, responseState) {
  const section = makeElement("section", "investigation-summary");
  section.setAttribute("aria-labelledby", "investigation-summary-heading");
  section.append(makeElement("h3", null, "Investigation result"));
  section.firstElementChild.id = "investigation-summary-heading";

  const badges = makeElement("div", "badge-row summary-badges");
  badges.append(
    makeStatusBadge(responseState),
    makeConfidenceBadge(data.overall_confidence || data.confidence),
  );
  section.append(badges);

  const metadata = makeElement("dl", "summary-metadata");
  metadata.append(
    makeDefinition("Investigation ID", scalarText(data.investigation_id)),
    makeDefinition("Schema version", scalarText(data.schema_version)),
  );
  section.append(metadata);
  return section;
}

function renderChain(data) {
  const section = makeElement("section", "chain-section");
  section.setAttribute("aria-labelledby", "chain-heading");
  const headingGroup = makeElement("div", "section-heading-row");
  const headingCopy = makeElement("div");
  headingCopy.append(
    makeElement("p", "scope-kicker", "Observed evidence"),
    makeElement("h3", null, "Seven-hop chain"),
  );
  headingCopy.lastElementChild.id = "chain-heading";
  headingGroup.append(
    headingCopy,
    makeElement("p", "section-note", "Requested input is intentionally excluded from observed values."),
  );
  section.append(headingGroup);

  const chain = makeElement("div", "chain");
  const hops = normalizeHops(data.hops);
  const links = normalizeLinks(data.links);
  HOP_DEFINITIONS.forEach((definition, index) => {
    chain.append(renderHop(hops[index], definition, index));
    if (index < HOP_DEFINITIONS.length - 1) {
      chain.append(renderLink(links[index], index));
    }
  });
  section.append(chain);
  return section;
}

function normalizeSources(data) {
  if (Array.isArray(data.sources)) {
    return data.sources;
  }
  if (isRecord(data.sources)) {
    return Object.entries(data.sources).map(([source, details]) => (
      isRecord(details) ? { ...details, source: details.source || source } : { source, status: details }
    ));
  }
  return asArray(data.query_outcomes);
}

function renderSources(data) {
  const section = makeElement("section", "sources-section");
  section.setAttribute("aria-labelledby", "sources-heading");
  section.append(makeElement("h3", null, "Telemetry sources"));
  section.firstElementChild.id = "sources-heading";
  section.append(
    makeElement(
      "p",
      "section-intro",
      "Query state and source failures are part of the attribution result.",
    ),
  );

  const sources = normalizeSources(data);
  if (!sources.length) {
    section.append(
      makeElement(
        "div",
        "empty-detail",
        "No source query state was included in the response.",
      ),
    );
    return section;
  }

  const list = makeElement("div", "source-list");
  sources.forEach((sourceValue) => {
    const source = isRecord(sourceValue) ? sourceValue : { source: sourceValue };
    const item = makeElement("article", "source-card");
    const header = makeElement("div", "source-card-header");
    header.append(
      makeElement("h4", null, scalarText(source.source || source.name, "Unnamed source")),
      makeStatusBadge(source.status || source.state || source.query_state),
    );
    item.append(header);

    const details = makeElement("dl", "source-metadata");
    if (typeof source.record_count === "number") {
      details.append(makeDefinition("Records", source.record_count));
    }
    if (typeof source.truncated === "boolean") {
      details.append(makeDefinition("Truncated", source.truncated ? "Yes" : "No"));
    }
    const error = scalarText(source.error || source.error_code, "");
    if (error) {
      details.append(makeDefinition("Source error", humanize(error)));
    }
    item.append(details);
    const sourceErrors = asArray(source.errors);
    if (sourceErrors.length) {
      const errorList = makeElement("ul", "source-error-list");
      sourceErrors.forEach((sourceError) => {
        if (isRecord(sourceError)) {
          const code = scalarText(sourceError.code || sourceError.error_code, "");
          const message = scalarText(sourceError.message || sourceError.detail, "");
          errorList.append(
            makeElement(
              "li",
              null,
              [code ? humanize(code) : "", message].filter(Boolean).join(": ")
                || "Unspecified source error",
            ),
          );
        } else {
          errorList.append(
            makeElement("li", null, humanize(scalarText(sourceError, "Unspecified source error"))),
          );
        }
      });
      item.append(errorList);
    }
    appendReasoning(item, source);
    list.append(item);
  });
  section.append(list);
  return section;
}

function normalizeNotices(data, responseState) {
  const supplied = Array.isArray(data.notices)
    ? data.notices
    : Array.isArray(data.uncertainty_notes)
      ? data.uncertainty_notes
      : asArray(data.reason_codes);
  const notices = supplied.map((notice) => (
    isRecord(notice)
      ? {
        title: scalarText(notice.title || notice.code, "Uncertainty notice"),
        detail: scalarText(notice.message || notice.detail || notice.explanation, ""),
        severity: normalizeToken(notice.severity || notice.level, "notice"),
      }
      : {
        title: humanize(scalarText(notice, "Uncertainty notice")),
        detail: "",
        severity: "notice",
      }
  ));

  if (responseState === "partial") {
    const hasCoverageNotice = notices.some((notice) => (
      normalizeToken(notice.title).includes("coverage")
      || normalizeToken(notice.title).includes("evidence-gap")
    ));
    if (!hasCoverageNotice) {
      notices.push({
        title: "Incomplete response or telemetry coverage",
        detail: "One or more expected hops, links, sources, or response fields were not reported.",
        severity: "warning",
      });
    }
  }
  return notices;
}

function renderNotices(data, responseState) {
  const notices = normalizeNotices(data, responseState);
  const section = makeElement("section", "notices-section");
  section.setAttribute("aria-labelledby", "notices-heading");
  section.append(makeElement("h3", null, "Uncertainty notices"));
  section.firstElementChild.id = "notices-heading";

  if (!notices.length) {
    section.append(
      makeElement(
        "p",
        "empty-detail",
        "No additional uncertainty notices were reported.",
      ),
    );
    return section;
  }

  const list = makeElement("ul", "notice-list");
  notices.forEach((notice) => {
    const item = makeElement("li", `notice notice-${statusClass(notice.severity)}`);
    item.append(makeElement("strong", null, notice.title));
    if (notice.detail) {
      item.append(makeElement("p", null, notice.detail));
    }
    list.append(item);
  });
  section.append(list);
  return section;
}

const LAST_HOP_COPY = {
  user_identified: "The API log names the user whose token performed the action.",
  workload_identified: "The API log names only the workload identity. The human is not in API telemetry; see the requester lead below and docs/ANALYST-PIVOT.md.",
  multiple_actors: "Different identities performed actions on this target. Review each action.",
  unresolved: "An action was found, but its identity binding is missing or contradictory.",
  no_evidence: "No API action in the selected permission mode was found for this target.",
};

function renderLastHop(data) {
  const last = isRecord(data.last_hop) ? data.last_hop : null;
  const section = makeElement("section", "last-hop-section");
  section.setAttribute("aria-labelledby", "last-hop-heading");
  section.append(makeElement("h3", null, "Last hop: who called the API"));
  section.firstElementChild.id = "last-hop-heading";
  if (!last) {
    section.append(makeElement("p", "empty-detail", "No last-hop assessment was included."));
    return section;
  }
  const verdict = scalarText(last.verdict, "unresolved");
  section.append(makeElement("p", "section-intro", LAST_HOP_COPY[verdict] || humanize(verdict)));
  const metadata = makeElement("dl", "summary-metadata");
  metadata.append(
    makeDefinition("Verdict", humanize(verdict)),
    makeDefinition("Binding strength", humanize(scalarText(last.strength, "none"))),
    makeDefinition("Human in API telemetry", humanize(scalarText(last.human, "unresolved"))),
    makeDefinition("Actions", scalarText(last.action_count, "0")),
  );
  section.append(metadata);
  const actions = asArray(last.actions);
  if (actions.length) {
    const list = makeElement("ul", "notice-list");
    actions.forEach((action) => {
      if (!isRecord(action)) {
        return;
      }
      const item = makeElement("li", "notice");
      item.append(makeElement(
        "strong", null,
        `${scalarText(action.event_time)} ${humanize(scalarText(action.action_category, "unknown"))} ${humanize(scalarText(action.resource_type, ""))}`,
      ));
      item.append(makeElement(
        "p", null,
        [
          `${humanize(scalarText(action.actor_type, "actor"))}: ${scalarText(action.actor_ref, "unknown")}`,
          `client app: ${scalarText(action.client_app_ref, "unknown")}`,
          `binding: ${humanize(scalarText(action.binding, "none"))}`,
          action.sign_in_ref ? `sign-in: ${scalarText(action.sign_in_ref)}` : "",
        ].filter(Boolean).join(" | "),
      ));
      list.append(item);
    });
    section.append(list);
  }
  appendReasoning(section, last);
  if (isRecord(last.requester_lead)) {
    section.append(renderRequesterLead(last.requester_lead));
  }
  return section;
}

const REQUESTER_LEAD_COPY = {
  sole_candidate: "One Copilot user prompted this agent just before the tool call that made this API request.",
  partial_candidate: "One Copilot user is the candidate for every action that has an Application Insights tool span. Older actions show \"No tool span\" because they ran before Application Insights was connected or are outside its data.",
  multiple_candidates: "More than one Copilot user prompted this agent in the window. Review each candidate.",
  no_tool_span: "No Application Insights tool span matches the time and target of these API actions.",
  no_agent_identity: "The tool span's agent blueprint has no agent identity sign-in in Entra logs.",
  no_agent_prompts: "No Copilot interaction with this agent was logged shortly before the tool call.",
  application_insights_unavailable: "No agent telemetry in Application Insights. Connect Application Insights to the Foundry project to enable this lead.",
  query_failed: "The requester lead query did not complete.",
  no_actions: "No API action to trace back.",
};

function renderRequesterLead(lead) {
  const block = makeElement("div", "requester-lead");
  block.append(makeElement("h4", null, "Requester lead (Application Insights + Entra + Copilot audit)"));
  const status = scalarText(lead.status, "query_failed");
  block.append(makeElement("p", "section-intro", REQUESTER_LEAD_COPY[status] || humanize(status)));
  block.append(makeElement(
    "p", "empty-detail",
    `Lead, not proof. Requires ${scalarText(lead.requires, "Application Insights connected to the Foundry project")}. `
    + "Graph to tool span and prompt to tool span are matched by target and time; the agent links are exact IDs.",
  ));
  const metadata = makeElement("dl", "summary-metadata");
  const agrees = lead.agrees_with_token;
  metadata.append(
    makeDefinition("Lead status", humanize(status)),
    makeDefinition("Candidate users", asArray(lead.user_refs).map((ref) => scalarText(ref)).join(", ") || "none"),
  );
  if (agrees === true || agrees === false) {
    metadata.append(makeDefinition("Matches token user", agrees ? "Yes" : "No"));
  }
  block.append(metadata);
  const list = makeElement("ul", "notice-list");
  asArray(lead.actions).forEach((action) => {
    if (!isRecord(action)) {
      return;
    }
    const offset = action.prompt_offset_seconds;
    list.append(makeElement("li", "notice", [
      `${scalarText(action.event_time, scalarText(action.evidence_ref))}: ${humanize(scalarText(action.status, "unknown"))}`,
      asArray(action.user_refs).length ? `user: ${asArray(action.user_refs).join(", ")}` : "",
      asArray(action.agent_identity_refs).length ? `agent identity: ${asArray(action.agent_identity_refs).join(", ")}` : "",
      asArray(action.conversation_refs).length ? `agent conversation: ${asArray(action.conversation_refs).join(", ")}` : "",
      typeof offset === "number" ? `prompt ${offset}s before tool call` : "",
    ].filter(Boolean).join(" | ")));
  });
  if (list.childElementCount) {
    block.append(list);
  }
  return block;
}

function renderInvestigation(data, request) {
  const responseState = stateForResponse(data);
  setState(responseState);
  clearNode(resultContent);
  resultContent.append(
    renderRequestedScope(data, request),
    renderInvestigationSummary(data, responseState),
    renderLastHop(data),
    renderNotices(data, responseState),
    renderChain(data),
    renderSources(data),
  );
  focusResults();
}

function failureReference(data) {
  if (!isRecord(data)) {
    return "";
  }
  return scalarText(data.error_id || data.investigation_id, "");
}

function renderServiceFailure(title, detail, data) {
  setState("service-failure", { title, detail });
  clearNode(resultContent);
  const reference = failureReference(data);
  if (reference) {
    const panel = makeElement("section", "failure-reference");
    panel.append(
      makeElement("h3", null, "Failure reference"),
      makeElement("p", null, reference),
    );
    resultContent.append(panel);
  }
  focusResults();
}

async function parseResponse(response) {
  const body = await response.text();
  if (!body.trim()) {
    return { kind: "empty", data: null };
  }
  try {
    return { kind: "json", data: JSON.parse(body) };
  } catch (error) {
    return { kind: "non-json", data: null };
  }
}

async function runInvestigation(request, requestId) {
  const controller = new AbortController();
  const requestState = {
    controller,
    id: requestId,
    timedOut: false,
    superseded: false,
  };
  activeRequest = requestState;
  const timeout = window.setTimeout(() => {
    requestState.timedOut = true;
    controller.abort();
  }, 150000);

  try {
    const response = await fetch("/api/investigate", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      cache: "no-store",
      credentials: "same-origin",
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    const parsed = await parseResponse(response);

    if (requestId !== requestSequence) {
      return;
    }
    if (parsed.kind === "empty") {
      renderServiceFailure(
        "Empty service response",
        "The local service returned no response body. No attribution result can be shown.",
        null,
      );
      return;
    }
    if (parsed.kind === "non-json") {
      renderServiceFailure(
        "Unexpected service response",
        "The local service returned a non-JSON response. No untrusted response content was displayed.",
        null,
      );
      return;
    }
    if (!isRecord(parsed.data)) {
      renderServiceFailure(
        "Invalid service response",
        "The local service returned JSON in an unsupported shape.",
        null,
      );
      return;
    }

    const status = normalizedResponseStatus(parsed.data);
    if (!response.ok) {
      if (status === "telemetry-unavailable" && responseHasInvestigationData(parsed.data)) {
        renderInvestigation(parsed.data, request);
        return;
      }
      const errorCode = scalarText(parsed.data.error, "");
      const errorDetail = errorCode
        ? `The service rejected the investigation (${humanize(errorCode)}).`
        : `The service returned HTTP ${response.status}.`;
      renderServiceFailure("Investigation request failed", errorDetail, parsed.data);
      return;
    }

    if (!responseHasInvestigationData(parsed.data)) {
      renderServiceFailure(
        "Empty investigation result",
        "The response did not contain status, chain, source, or uncertainty data.",
        parsed.data,
      );
      return;
    }
    renderInvestigation(parsed.data, request);
  } catch (error) {
    if (requestState.superseded || requestId !== requestSequence) {
      return;
    }
    if (requestState.timedOut) {
      renderServiceFailure(
        "Investigation timed out",
        "The local service did not respond within 60 seconds. No result was persisted.",
        null,
      );
      return;
    }
    renderServiceFailure(
      "Local service unreachable",
      "A network error prevented the investigation. Confirm the local service is running and try again.",
      null,
    );
  } finally {
    window.clearTimeout(timeout);
    if (requestId === requestSequence) {
      activeRequest = null;
      setBusy(false);
    }
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  clearValidation();

  const target = targetInput.value.trim();
  if (!target) {
    showValidation("Enter a target containing at least one visible character.");
    return;
  }
  if (target.length > 256) {
    showValidation("The requested target must be 256 characters or fewer.");
    return;
  }

  if (activeRequest) {
    activeRequest.superseded = true;
    activeRequest.controller.abort();
  }

  requestSequence += 1;
  const request = {
    target,
    mode: modeInput.value,
    timespan: timespanInput.value,
  };
  clearNode(resultContent);
  setState("loading");
  setBusy(true);
  runInvestigation(request, requestSequence);
});

targetInput.addEventListener("input", () => {
  if (targetInput.hasAttribute("aria-invalid")) {
    clearValidation();
  }
});

async function loadServiceHealth() {
  try {
    const response = await fetch("/api/health", {
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
    });
    const parsed = await parseResponse(response);
    if (!response.ok || parsed.kind !== "json" || !isRecord(parsed.data)) {
      serviceStatus.textContent = "Local service health unavailable";
      serviceStatus.className = "service-status service-unknown";
      return;
    }
    if (parsed.data.ready) {
      serviceStatus.textContent = "Telemetry adapter ready";
      serviceStatus.className = "service-status service-ready";
    } else {
      serviceStatus.textContent = "Telemetry adapter not ready";
      serviceStatus.className = "service-status service-warning";
    }
  } catch (error) {
    serviceStatus.textContent = "Local service unreachable";
    serviceStatus.className = "service-status service-error";
  }
}

loadServiceHealth();

"""Deterministic adjacent-edge assessments, without identity guessing."""

from dataclasses import dataclass, field, replace
from datetime import datetime

from .models import HOPS, MODES, EvidenceRecord, QueryOutcome
from .queries import QUERY_PLANS
from .requester_lead import assess_requester_lead

MATCH_WINDOW_SECONDS = 300
CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
EXACT_TYPES = (
    "request_id", "original_request_id", "client_request_id", "operation_id", "trace_id", "span_id",
    "token_id", "correlation_id", "session_id", "conversation_id", "thread_id",
    "message_id", "task_id",
)
STABLE_TYPES = ("user_id", "app_id", "service_principal_id", "agent_id", "target_id", "resource_id")
JOIN_PAIRS = (
    ("client_request_id", "correlation_id"),
    ("correlation_id", "client_request_id"), ("span_id", "parent_span_id"),
)


@dataclass(frozen=True)
class LinkResult:
    source: str
    target: str
    status: str
    confidence: str
    key_types: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    time_delta_seconds: float | None = None
    evidence_refs: tuple[str, ...] = ()
    query_outcomes: tuple[dict, ...] = ()
    pair: tuple[EvidenceRecord, EvidenceRecord] | None = field(default=None, repr=False, compare=False)

    @property
    def join_type(self):
        if self.status == "unavailable" or "evidence_gap" in self.reason_codes:
            return "no_evidence"
        if "exact_identifier" in self.reason_codes:
            return "exact_identifier"
        if set(self.reason_codes) & {"stable_temporal", "candidate_only"}:
            return "heuristic_time_window"
        return "unresolved"

    def public(self):
        return {
            "source": self.source, "target": self.target, "status": self.status,
            "confidence": self.confidence, "key_types": list(self.key_types),
            "join_type": self.join_type,
            "reason_codes": list(self.reason_codes),
            "time_delta_seconds": self.time_delta_seconds,
            "time_window_seconds": MATCH_WINDOW_SECONDS,
            "evidence_refs": list(self.evidence_refs),
            "query_outcomes": list(self.query_outcomes),
        }


@dataclass(frozen=True)
class HopResult:
    id: str
    key: str
    name: str
    status: str
    confidence: str
    evidence: tuple[dict, ...]
    reason_codes: tuple[str, ...] = ()

    def public(self):
        return {
            "id": self.id, "key": self.key, "name": self.name, "status": self.status,
            "confidence": self.confidence, "evidence": list(self.evidence),
            "reason_codes": list(self.reason_codes),
        }


def _values(record, kind):
    return set(record.ids.get(kind, ()))


def _pair_assessment(left, right):
    delta = (datetime.fromisoformat(right.event_time.replace("Z", "+00:00")) -
             datetime.fromisoformat(left.event_time.replace("Z", "+00:00"))).total_seconds()
    if any(r.evidence_type == "sign_in" and r.outcome == "failed" for r in (left, right)):
        return 0, (), "unsuccessful_event", delta
    if left.hop == "H5" and right.hop == "H6":
        if left.permission_mode not in MODES or right.permission_mode not in MODES:
            return 0, (), "unknown_permissions", delta
        if left.permission_mode != right.permission_mode:
            return 0, (), "permission_mode_mismatch", delta
        if right.permission_mode == "delegated_permissions" and (
            left.actor_type != "user" or not (_values(left, "user_id") & _values(right, "user_id"))
        ):
            return 0, (), "persisted_user_required", delta
        if right.permission_mode == "application_permissions" and left.actor_type not in {"service_principal", "managed_identity"}:
            return 0, (), "permission_mode_mismatch", delta
    if delta < -30 or delta > MATCH_WINDOW_SECONDS:
        return 0, (), "outside_time_window", delta
    if right.hop == "H7" and "target_context_only" in right.reason_codes and not (
        right.ids.get("target_id") or right.ids.get("resource_id")
    ):
        return 1, (), "target_context_only", delta
    conflicts = [
        kind for kind in (*EXACT_TYPES, "user_id", "app_id", "service_principal_id")
        if kind != "span_id"
        if _values(left, kind) and _values(right, kind) and not (_values(left, kind) & _values(right, kind))
    ]
    for a, b in JOIN_PAIRS:
        if _values(left, a) and _values(right, b) and not (_values(left, a) & _values(right, b)):
            conflicts.extend((a, b))
    if conflicts:
        return 0, tuple(conflicts), "contradictory_ids", delta
    shared = {
        kind for kind in EXACT_TYPES
        if _values(left, kind) & _values(right, kind)
    }
    for a, b in JOIN_PAIRS:
        if _values(left, a) & _values(right, b):
            shared.update((a, b))
    if left.source == right.source and _values(left, "source_event_id") & _values(right, "source_event_id"):
        shared.add("source_event_id")
    if shared:
        return 4, tuple(sorted(shared)), "exact_identifier", delta
    stable = tuple(kind for kind in STABLE_TYPES if _values(left, kind) & _values(right, kind))
    # Actor + application + time is deliberately insufficient. A third independent
    # stable dimension is needed, and uniqueness is assessed across all candidates.
    if len(stable) >= 3 and set(stable) & {"target_id", "resource_id", "agent_id"}:
        return 2, stable, "stable_temporal", delta
    if stable:
        return 1, stable, "candidate_only", delta
    binding = set(EXACT_TYPES) | set(STABLE_TYPES)
    comparable = bool(set(left.ids) & set(right.ids) & binding) or any(
        _values(left, a) and _values(right, b) for a, b in JOIN_PAIRS
    )
    reason = "no_shared_ids" if comparable else "no_binding_keys"
    return 0, (), reason, delta


def _assess_edge(source, target, left, right, outcomes, refs):
    relevant = tuple(
        outcome for outcome in outcomes
        if source in QUERY_PLANS[outcome.source].hops or target in QUERY_PLANS[outcome.source].hops
        or any(r.hop in {source, target} for r in outcome.records)
    )
    coverage = tuple(o.public() for o in relevant)
    if not left or not right:
        return LinkResult(source, target, "unavailable", "none", reason_codes=("evidence_gap",), query_outcomes=coverage)
    assessments = [(a, b, _pair_assessment(a, b)) for a in left for b in right]
    best_score = max(result[2][0] for result in assessments)
    best = [result for result in assessments if result[2][0] == best_score]
    if not best_score:
        reasons = {result[2][2] for result in assessments}
        reason = "contradictory_ids" if "contradictory_ids" in reasons else (
            "permission_mode_mismatch" if "permission_mode_mismatch" in reasons else
            "persisted_user_required" if "persisted_user_required" in reasons else
            "unknown_permissions" if "unknown_permissions" in reasons else
            "unsuccessful_event" if "unsuccessful_event" in reasons else
            "no_binding_keys" if "no_binding_keys" in reasons else
            "outside_time_window" if reasons == {"outside_time_window"} else "no_shared_ids"
        )
        return LinkResult(source, target, "unresolved", "none", reason_codes=(reason,), query_outcomes=coverage)
    if len(best) != 1:
        return LinkResult(source, target, "unresolved", "none", reason_codes=("ambiguous",), query_outcomes=coverage)
    a, b, (_, keys, reason, delta) = best[0]
    if any(o.truncated or o.status != "succeeded" for o in relevant if o.required):
        return LinkResult(
            source, target, "unresolved", "low", key_types=keys,
            reason_codes=("incomplete_coverage",), query_outcomes=coverage,
        )
    return LinkResult(
        source, target, "linked" if best_score >= 2 else "candidate",
        {1: "low", 2: "medium", 3: "medium", 4: "high"}[best_score],
        keys, (reason,), delta, (refs[a.fingerprint()], refs[b.fingerprint()]),
        coverage, (a, b) if best_score >= 2 else None,
    )


USER_SIGN_IN_SOURCES = frozenset({"SigninLogs", "AADNonInteractiveUserSignInLogs"})
WORKLOAD_SIGN_IN_SOURCES = frozenset({"AADServicePrincipalSignInLogs", "AADManagedIdentitySignInLogs"})
MAX_LAST_HOP_ACTIONS = 12


def _last_hop(by_hop, mode, references, opaque):
    """Who presented the token for each API action, independent of upstream hops.

    Proof is native only: the Graph row's own identity, optionally corroborated by
    the sign-in row whose UniqueTokenIdentifier equals the Graph SignInActivityId.
    """
    graph = tuple(r for r in by_hop["H6"] if r.source == "MicrosoftGraphActivityLogs")
    actions = tuple(r for r in graph if r.permission_mode == mode)
    sign_ins = tuple(r for r in by_hop["H5"] if r.outcome != "failed")
    reasons = set()
    if any(r.permission_mode in {"mixed", "unknown"} for r in graph):
        reasons.add("mixed_permissions" if any(r.permission_mode == "mixed" for r in graph) else "unknown_permissions")
    if any(r.permission_mode in MODES and r.permission_mode != mode for r in graph):
        reasons.add("permission_mode_mismatch")
    entries, actors = [], set()
    for action in actions[:MAX_LAST_HOP_ACTIONS]:
        if mode == "delegated_permissions":
            actor_kind, sources = "user_id", USER_SIGN_IN_SOURCES
        else:
            actor_kind = "service_principal_id" if _values(action, "service_principal_id") else "app_id"
            sources = WORKLOAD_SIGN_IN_SOURCES
        actor_values = _values(action, actor_kind)
        token = [
            s for s in sign_ins
            if s.source in sources and _values(s, "token_id") & _values(action, "token_id")
        ]
        conflicting = [s for s in token if _values(s, actor_kind) and not (_values(s, actor_kind) & actor_values)]
        corroborated = [s for s in token if _values(s, actor_kind) & actor_values]
        actor = min(actor_values) if len(actor_values) == 1 else None
        if conflicting or len(actor_values) > 1:
            binding = "contradictory_ids"
            actor = None
        elif actor is None:
            binding = "no_actor_identity"
        elif corroborated:
            binding = "token_exact"
        else:
            binding = "api_log_only"
        if actor:
            actors.add((actor_kind, actor))
        apps = _values(action, "app_id")
        entries.append({
            "evidence_ref": references[action.fingerprint()],
            "event_time": action.event_time,
            "action_category": action.action_category,
            "resource_type": action.resource_type,
            "outcome": action.outcome,
            "actor_type": "user" if mode == "delegated_permissions" else "workload",
            "actor_ref": opaque[(actor_kind, actor)] if actor else None,
            "client_app_ref": opaque[("app_id", min(apps))] if len(apps) == 1 else None,
            "binding": binding,
            "sign_in_ref": references[corroborated[0].fingerprint()] if corroborated else None,
        })
    bindings = {entry["binding"] for entry in entries}
    if not entries:
        verdict = "no_evidence"
        reasons.add("evidence_gap")
    elif bindings & {"contradictory_ids", "no_actor_identity"}:
        verdict = "unresolved"
        reasons.update(bindings & {"contradictory_ids"})
    elif len(actors) > 1:
        verdict = "multiple_actors"
    elif mode == "delegated_permissions":
        verdict = "user_identified"
    else:
        verdict = "workload_identified"
    strength = (
        "token_exact" if entries and bindings == {"token_exact"} else
        "api_log_only" if entries and bindings <= {"token_exact", "api_log_only"} else "none"
    )
    return {
        "verdict": verdict,
        "mode": mode,
        "strength": strength,
        "human": (
            "in_api_telemetry" if verdict == "user_identified" else
            "not_in_api_telemetry" if mode == "application_permissions" and entries else "unresolved"
        ),
        "action_count": len(actions),
        "truncated": len(actions) > MAX_LAST_HOP_ACTIONS,
        "actions": entries,
        "reason_codes": sorted(reasons),
    }


def correlate(outcomes, mode="delegated_permissions", lead=None):
    if mode not in MODES:
        raise ValueError("invalid_request")
    outcomes = tuple(outcomes)
    if not all(isinstance(outcome, QueryOutcome) for outcome in outcomes):
        raise ValueError("invalid_outcome")
    outcomes = tuple(sorted(outcomes, key=lambda o: (o.source, o.status, o.reason_codes)))
    unique = {record.fingerprint(): record for outcome in outcomes for record in outcome.records}
    records = tuple(unique[k] for k in sorted(unique))
    references = {r.fingerprint(): f"evidence-{i:03d}" for i, r in enumerate(records, 1)}
    identifiers = sorted({(kind, value) for record in records for kind, values in record.ids.items() for value in values})
    opaque = {key: f"corr-{i:03d}" for i, key in enumerate(identifiers, 1)}
    by_hop = {hop[0]: tuple(r for r in records if r.hop == hop[0]) for hop in HOPS}
    links = [
        _assess_edge(HOPS[i][0], HOPS[i + 1][0], by_hop[HOPS[i][0]], by_hop[HOPS[i + 1][0]], outcomes, references)
        for i in range(6)
    ]
    reasons = set()
    graph = by_hop["H6"]
    matching_graph = tuple(r for r in graph if r.permission_mode == mode)
    if any(r.permission_mode == "mixed" for r in graph):
        reasons.add("mixed_permissions")
    if any(r.permission_mode == "unknown" for r in graph):
        reasons.add("unknown_permissions")
    if any(r.permission_mode in MODES and r.permission_mode != mode for r in graph):
        reasons.add("permission_mode_mismatch")
    permission_reasons = reasons & {"mixed_permissions", "unknown_permissions", "permission_mode_mismatch"}
    if permission_reasons or not matching_graph:
        links[4] = replace(
            links[4], status="unresolved" if graph else "unavailable", confidence="none",
            reason_codes=tuple(sorted(permission_reasons or {"evidence_gap"})), pair=None,
        )
    user_evidence = tuple(
        r for r in by_hop["H1"]
        if r.actor_type == "user" and r.ids.get("user_id")
        and r.evidence_type in {"sign_in", "agent_activity"}
        and r.outcome != "failed"
    )
    delegated_user = any(
        _values(user, "user_id") & _values(action, "user_id")
        for user in user_evidence for action in matching_graph
    )
    if links[0].pair and links[4].pair:
        first, action = links[0].pair[0], links[4].pair[1]
        delegated_user = first in user_evidence and bool(_values(first, "user_id") & _values(action, "user_id"))
    if mode == "delegated_permissions" and not delegated_user:
        reasons.add("persisted_user_required")
        links[0] = replace(
            links[0], status="unresolved" if by_hop["H1"] else "unavailable",
            confidence="none", reason_codes=("persisted_user_required",), pair=None,
        )
    if mode == "application_permissions" and (not user_evidence or not all(link.status == "linked" for link in links[:5])):
        reasons.add("backward_human_link_required")
    coherent = all(
        links[i].pair is not None and links[i + 1].pair is not None
        and links[i].pair[1].fingerprint() == links[i + 1].pair[0].fingerprint()
        for i in range(5)
    )
    all_linked = all(link.status == "linked" for link in links)
    if all_linked and not coherent:
        reasons.add("incoherent_chain")
        links[0] = replace(
            links[0], status="unresolved", confidence="none",
            reason_codes=("incoherent_chain",), pair=None,
        )
    required = tuple(o for o in outcomes if o.required)
    any_queryable = any(o.status == "succeeded" for o in required)
    incomplete = any(o.status != "succeeded" or o.truncated for o in required)
    if incomplete:
        reasons.add("incomplete_coverage")
    if not any_queryable:
        status = "unavailable"
        reasons.add("no_required_source")
    elif not records:
        status = "partial" if incomplete else "no_evidence"
    elif all_linked and coherent and matching_graph and not reasons:
        status = "complete"
    elif incomplete or any(not by_hop[hop[0]] for hop in HOPS):
        status = "partial"
        reasons.add("evidence_gap")
    elif (
        mode == "application_permissions" and matching_graph
        and any("no_binding_keys" in link.reason_codes for link in links)
        and not any(set(link.reason_codes) & {
            "ambiguous", "contradictory_ids", "unknown_permissions",
            "permission_mode_mismatch", "persisted_user_required", "unsuccessful_event",
            "outside_time_window",
        } for link in links)
        and not reasons & {"mixed_permissions", "permission_mode_mismatch", "unknown_permissions"}
    ):
        status = "impossible"
        reasons.add("structurally_unlinkable")
    else:
        status = "unresolved"
    confidence = min((link.confidence for link in links), key=CONFIDENCE_ORDER.__getitem__)

    def public_record(record):
        preferred = next((kind for kind in EXACT_TYPES if record.ids.get(kind)), None)
        return {
            "id": references[record.fingerprint()], "source": record.source,
            "hop": record.hop, "event_time": record.event_time,
            "evidence_type": record.evidence_type, "actor_type": record.actor_type,
            "action_category": record.action_category, "resource_type": record.resource_type,
            "correlation_key_type": preferred,
            "correlation_ref": opaque[(preferred, record.ids[preferred][0])] if preferred else None,
            "outcome": record.outcome, "confidence": "low",
            "reason_codes": list(record.reason_codes),
        }

    last_hop = _last_hop(by_hop, mode, references, opaque)
    if lead is not None:
        # Kept beside the verdict, never folded into it: two lead links are target/time matches.
        def ref(kind, value):
            return opaque.setdefault((kind, value), f"corr-{len(opaque) + 1:03d}")

        actions = tuple(
            r for r in by_hop["H6"]
            if r.source == "MicrosoftGraphActivityLogs" and r.permission_mode == mode
        )[:MAX_LAST_HOP_ACTIONS]
        proven = None
        if mode == "delegated_permissions":
            proven = [entry["actor_ref"] for entry in last_hop["actions"] if entry["actor_ref"]]
        last_hop["requester_lead"] = assess_requester_lead(actions, lead, references, ref, proven)

    hops = []
    for index, (hop_id, key, name) in enumerate(HOPS):
        evidence = by_hop[hop_id]
        related = links[max(0, index - 1):min(6, index + 1)]
        node_confidence = min((link.confidence for link in related), key=CONFIDENCE_ORDER.__getitem__)
        hops.append(HopResult(
            hop_id, key, name, "observed" if evidence else "unavailable",
            node_confidence if evidence else "none",
            tuple(public_record(record) for record in evidence[:12]),
            () if evidence else ("evidence_gap",),
        ).public())
    return {
        "status": status, "mode": mode, "confidence": confidence,
        "hops": hops, "links": [link.public() for link in links],
        "query_outcomes": [outcome.public() for outcome in outcomes],
        "reason_codes": sorted(reasons),
        "human_attribution": "supported" if status == "complete" else "unresolved",
        "last_hop": last_hop,
    }


correlate_chain = correlate

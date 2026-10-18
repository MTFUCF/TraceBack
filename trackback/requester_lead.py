"""Requester lead: which Copilot user prompted the agent that made a Graph call.

Built only from Microsoft platform logs, and only when Application Insights is
connected to the Foundry project (an out-of-box project setting):

  Graph action --(same target, inside the span's time range)--> AppDependencies execute_tool span
  span microsoft.a365.agent.blueprint.id ==(exact)== AADServicePrincipalSignInLogs Agent.parentAppId
  that sign-in's ServicePrincipalId (agent identity) ==(exact)== CopilotActivity AgentId suffix
  CopilotInteraction prompts to that agent shortly before the span --> ActorUserId

Two of the links are target/time matches, not shared identifiers, so this is a
lead for an analyst to confirm. It never changes the last-hop verdict.
"""

from dataclasses import dataclass
from datetime import datetime
import re

from .models import TIMESPANS, InvestigationRequest, safe_id
from .queries import configured_table_name, encode_scalar

BASIS = "application_insights_agent_trace"
REQUIREMENT = "Application Insights connected to the Foundry project"
MAX_TOOL_ROWS = 50
MAX_AGENT_ROWS = 20
MAX_PROMPT_ROWS = 100
TOOL_SLACK_SECONDS = 5
PROMPT_BEFORE_SECONDS = 900
PROMPT_AFTER_SECONDS = 60
KINDS = frozenset({"coverage", "tool", "agent", "prompt"})
NON_USER_TYPES = frozenset({"", "system", "application", "serviceprincipal", "service_principal"})
LINKS = (
    {"from": "graph_action", "to": "agent_tool_span", "join": "target_and_time"},
    {"from": "agent_tool_span", "to": "agent_identity_sign_in", "join": "blueprint_exact"},
    {"from": "agent_identity_sign_in", "to": "copilot_interaction", "join": "agent_identity_exact"},
    {"from": "copilot_interaction", "to": "user", "join": "agent_and_time"},
)
_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_COLUMNS = (
    "Kind", "TimeGenerated", "DurationMs", "Blueprint", "AgentIdentity",
    "UserId", "UserType", "ConversationId", "SpanId",
)


@dataclass(frozen=True)
class LeadRows:
    status: str
    reason: str | None = None
    rows: tuple[dict, ...] = ()


def build_lead_query(target, timespan="P30D"):
    request = InvestigationRequest(target, "application_permissions", timespan)
    spans = configured_table_name("AppDependencies")
    sign_ins = configured_table_name("AADServicePrincipalSignInLogs")
    copilot = configured_table_name("CopilotActivity")
    empty = "Kind = {kind}, TimeGenerated, DurationMs = {duration}, " + ", ".join(
        f"{name} = {{{name}}}" for name in _COLUMNS[3:]
    )

    def project(kind, duration="real(null)", **values):
        fields = {name: '""' for name in _COLUMNS[3:]}
        fields.update(values)
        return "| project " + empty.format(kind=f'"{kind}"', duration=duration, **fields)

    return "\n".join((
        f"let _target = {encode_scalar(request.target)};",
        "let _target_enc = url_encode_component(_target);",
        "let _target_oid = toscalar(union isfuzzy=true (datatable(AccountObjectId:string)[]), "
        "(IdentityInfo | where TimeGenerated >= ago(14d) and AccountUPN =~ _target "
        "| project AccountObjectId = tostring(AccountObjectId)) "
        "| where isnotempty(AccountObjectId) | summarize take_any(AccountObjectId));",
        f"let _since = ago({TIMESPANS[request.timespan]});",
        "let _spans = union isfuzzy=true "
        "(datatable(TimeGenerated:datetime, DurationMs:real, Properties:dynamic, Id:string)[]), "
        f"({spans} | where TimeGenerated >= _since and TimeGenerated <= now())",
        '| where isnotempty(tostring(Properties["gen_ai.operation.name"]));',
        "let _tools = _spans",
        '| where tostring(Properties["gen_ai.operation.name"]) == "execute_tool"',
        '| extend _args = tostring(Properties["gen_ai.tool.call.arguments"]), '
        '_result = tostring(Properties["gen_ai.tool.call.result"])',
        "| where _args contains _target or _args contains _target_enc or _result contains _target "
        "or (isnotempty(_target_oid) and (_args contains _target_oid or _result contains _target_oid))",
        project(
            "tool", duration="todouble(DurationMs)",
            Blueprint='tostring(Properties["microsoft.a365.agent.blueprint.id"])',
            ConversationId='substring(tostring(Properties["gen_ai.conversation.id"]), 0, 512)',
            SpanId="substring(tostring(Id), 0, 128)",
        ),
        f"| top {MAX_TOOL_ROWS} by TimeGenerated desc;",
        "let _blueprints = _tools | where isnotempty(Blueprint) | distinct Blueprint;",
        "let _agents = union isfuzzy=true "
        "(datatable(TimeGenerated:datetime, ServicePrincipalId:string)[]), "
        f"({sign_ins} | where TimeGenerated >= _since and TimeGenerated <= now())",
        '| extend _blueprint = tostring(parse_json(tostring(column_ifexists("Agent", ""))).parentAppId)',
        "| where _blueprint in (_blueprints) and isnotempty(ServicePrincipalId)",
        "| summarize TimeGenerated = max(TimeGenerated) by _blueprint, _identity = tostring(ServicePrincipalId)",
        project("agent", Blueprint="_blueprint", AgentIdentity="_identity"),
        f"| take {MAX_AGENT_ROWS};",
        "let _identities = _agents | distinct AgentIdentity;",
        f"let _start = toscalar(_tools | summarize min(TimeGenerated)) - {PROMPT_BEFORE_SECONDS}s;",
        "let _end = toscalar(_tools | summarize max(TimeGenerated + DurationMs * 1ms)) "
        f"+ {PROMPT_AFTER_SECONDS}s;",
        "let _prompts = union isfuzzy=true (datatable(TimeGenerated:datetime)[]), "
        f"({copilot} | where TimeGenerated between (_start .. _end))",
        '| where tostring(column_ifexists("RecordType", "")) == "CopilotInteraction"',
        "| extend _identity = extract(@\"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        "[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})$\", 1, tostring(column_ifexists(\"AgentId\", \"\")))",
        "| where _identity in (_identities)",
        project(
            "prompt", AgentIdentity="_identity",
            UserId='tostring(column_ifexists("ActorUserId", ""))',
            UserType='tostring(column_ifexists("ActorUserType", ""))',
        ),
        f"| top {MAX_PROMPT_ROWS} by TimeGenerated desc;",
        "let _coverage = _spans | summarize _count = count() | extend TimeGenerated = now()",
        project("coverage", duration="todouble(_count)") + ";",
        "union _coverage, _tools, _agents, _prompts",
        "| order by Kind asc, TimeGenerated asc",
    ))


def _time(value):
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_record")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid_record")
    return parsed


def _guid(value):
    return value.casefold() if isinstance(value, str) and _GUID.fullmatch(value) else None


def _parse(rows):
    parsed = {kind: [] for kind in KINDS}
    invalid = False
    for row in rows:
        try:
            kind = row.get("Kind") if isinstance(row, dict) else None
            if kind not in KINDS:
                raise ValueError("invalid_record")
            item = {"time": _time(row.get("TimeGenerated"))}
            if kind == "coverage":
                item["count"] = int(row.get("DurationMs") or 0)
            elif kind == "tool":
                duration = float(row.get("DurationMs") or 0)
                if not 0 <= duration <= 3_600_000:
                    raise ValueError("invalid_record")
                item.update(
                    duration=duration / 1000, blueprint=_guid(row.get("Blueprint")),
                    conversation=safe_id(row.get("ConversationId")), span=safe_id(row.get("SpanId")),
                )
            elif kind == "agent":
                item.update(blueprint=_guid(row.get("Blueprint")), identity=_guid(row.get("AgentIdentity")))
                if not item["blueprint"] or not item["identity"]:
                    raise ValueError("invalid_record")
            else:
                user_type = row.get("UserType") if isinstance(row.get("UserType"), str) else ""
                item.update(
                    identity=_guid(row.get("AgentIdentity")), user=_guid(row.get("UserId")),
                    human=user_type.replace(" ", "").casefold() not in NON_USER_TYPES,
                )
                if not item["identity"]:
                    raise ValueError("invalid_record")
            parsed[kind].append(item)
        except (ValueError, TypeError, OverflowError, AttributeError):
            invalid = True
    return parsed, invalid


def _assess_action(action_time, parsed, ref):
    tools = [
        t for t in parsed["tool"]
        if (t["time"] - action_time).total_seconds() <= TOOL_SLACK_SECONDS
        and (action_time - t["time"]).total_seconds() <= t["duration"] + TOOL_SLACK_SECONDS
    ]
    result = {"status": "no_tool_span", "user_refs": [], "tool_span_refs": [], "conversation_refs": [],
              "agent_identity_refs": [], "prompt_offset_seconds": None}
    if not tools:
        return result, set()
    result["tool_span_refs"] = sorted(ref("span_id", t["span"]) for t in tools if t["span"])
    result["conversation_refs"] = sorted({ref("conversation_id", t["conversation"]) for t in tools if t["conversation"]})
    blueprints = {t["blueprint"] for t in tools if t["blueprint"]}
    identities = {a["identity"] for a in parsed["agent"] if a["blueprint"] in blueprints}
    result["agent_identity_refs"] = sorted(ref("service_principal_id", i) for i in identities)
    if not identities:
        result["status"] = "no_agent_identity"
        return result, set()
    start = min(t["time"] for t in tools)
    prompts = [
        p for p in parsed["prompt"]
        if p["identity"] in identities and p["human"] and p["user"]
        and -PROMPT_AFTER_SECONDS <= (start - p["time"]).total_seconds() <= PROMPT_BEFORE_SECONDS
    ]
    if not prompts:
        result["status"] = "no_agent_prompts"
        return result, set()
    users = {p["user"] for p in prompts}
    result["status"] = "sole_candidate" if len(users) == 1 else "multiple_candidates"
    result["user_refs"] = sorted(ref("user_id", u) for u in users)
    offsets = [(start - p["time"]).total_seconds() for p in prompts]
    before = [o for o in offsets if o >= 0]
    result["prompt_offset_seconds"] = round(min(before) if before else max(offsets), 3)
    return result, users


def assess_requester_lead(actions, lead, references, ref, proven_user_refs=None):
    """actions: Graph EvidenceRecords from the last hop; ref(kind, value) -> opaque ref."""
    output = {
        "basis": BASIS, "requires": REQUIREMENT, "proof": False, "status": "query_failed",
        "user_refs": [], "agrees_with_token": None, "actions": [], "links": [dict(link) for link in LINKS],
        "reason_codes": [],
    }
    if lead.status != "succeeded":
        output["status"] = "application_insights_unavailable" if lead.status == "unavailable" else "query_failed"
        output["reason_codes"] = [lead.reason] if lead.reason else []
        return output
    parsed, invalid = _parse(lead.rows)
    if invalid:
        output["reason_codes"].append("invalid_record")
    if not sum(c["count"] for c in parsed["coverage"]):
        output["status"] = "application_insights_unavailable"
        return output
    if not actions:
        output["status"] = "no_actions"
        return output
    statuses, all_users = [], set()
    for action in actions:
        entry, users = _assess_action(_time(action.event_time), parsed, ref)
        entry["evidence_ref"] = references[action.fingerprint()]
        entry["event_time"] = action.event_time
        output["actions"].append(entry)
        statuses.append(entry["status"])
        all_users |= users
    if len(all_users) > 1 or "multiple_candidates" in statuses:
        output["status"] = "multiple_candidates"
    elif all_users:
        output["status"] = "sole_candidate" if set(statuses) == {"sole_candidate"} else "partial_candidate"
    else:
        output["status"] = next(s for s in ("no_tool_span", "no_agent_identity", "no_agent_prompts") if s in statuses)
    output["user_refs"] = sorted(ref("user_id", u) for u in all_users)
    if proven_user_refs is not None and output["user_refs"]:
        output["agrees_with_token"] = set(output["user_refs"]) == set(proven_user_refs)
    return output

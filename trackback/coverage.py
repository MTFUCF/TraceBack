"""Telemetry coverage self-check: can the workspace actually hold the evidence?

An empty investigation is only meaningful when the native sources are being
ingested unfiltered. This module reports configuration and ingestion gaps with
fixed reason codes; it never returns query rows, KQL text or account details.
"""

import json
import subprocess
import time

from .adapter import AzureCLIAdapter, OutputLimitExceeded, WorkspaceConfigurationError, classify_failure
from .queries import QUERY_PLANS, configured_table_name

COVERAGE_TIMEOUT = 90
WORKSPACE_API = "2023-09-01"
DCR_API = "2023-03-11"
ENTRA_DIAGNOSTICS_URL = "https://management.azure.com/providers/microsoft.aadiam/diagnosticSettings?api-version=2017-04-01"
# Entra diagnostic categories that feed the required native sources.
REQUIRED_ENTRA_CATEGORIES = (
    "MicrosoftGraphActivityLogs", "SignInLogs", "NonInteractiveUserSignInLogs",
    "ServicePrincipalSignInLogs", "ManagedIdentitySignInLogs", "AuditLogs",
)
# Native sources without which last-hop attribution is impossible.
CORE_SOURCES = (
    "MicrosoftGraphActivityLogs", "AADNonInteractiveUserSignInLogs", "SigninLogs",
    "AADManagedIdentitySignInLogs", "AADServicePrincipalSignInLogs",
)
STATUS_ORDER = {"ok": 0, "unknown": 1, "warn": 2, "fail": 3}


def _check(check_id, status, reason, **detail):
    return {"id": check_id, "status": status, "reason_code": reason, "detail": detail}


class CoverageChecker:
    def __init__(self, adapter=None):
        self.adapter = adapter if adapter is not None else AzureCLIAdapter()

    def _json(self, args, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "budget_exhausted"
        try:
            code, output, error = self.adapter._run(args, min(self.adapter.timeout, remaining))
            if code:
                return None, classify_failure(error)[1]
            return json.loads(output), None
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except OutputLimitExceeded:
            return None, "output_limit"
        except OSError:
            return None, "cli_unavailable"
        except (ValueError, UnicodeError, RecursionError):
            return None, "invalid_response"

    def _query(self, customer_id, query, deadline):
        payload, failure = self._json([
            "monitor", "log-analytics", "query", "--workspace", customer_id,
            "--subscription", self.adapter.workspace.subscription_id,
            "--analytics-query", query, "--timespan", "P7D", "--output", "json", "--only-show-errors",
        ], deadline)
        if failure:
            return None, failure
        if isinstance(payload, dict) and isinstance(payload.get("tables"), list) and payload["tables"]:
            table = payload["tables"][0]
            names = [c.get("name") for c in table.get("columns", [])]
            payload = [dict(zip(names, row)) for row in table.get("rows", [])]
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            return None, "invalid_response"
        return payload, None

    def run(self):
        if not isinstance(self.adapter, AzureCLIAdapter):
            return _summary([_check("adapter", "unknown", "coverage_requires_azure_cli_adapter")])
        if self.adapter.configuration_error is not None:
            raise WorkspaceConfigurationError(self.adapter.configuration_error)
        if not self.adapter.command:
            return _summary([_check("azure_cli", "fail", "cli_unavailable")])
        deadline = time.monotonic() + COVERAGE_TIMEOUT
        workspace = self.adapter.workspace
        checks = []
        info, failure = self._json([
            "rest", "--method", "get", "--url",
            f"https://management.azure.com{workspace.resource_id}?api-version={WORKSPACE_API}",
            "--output", "json", "--only-show-errors",
        ], deadline)
        properties = info.get("properties", {}) if isinstance(info, dict) else {}
        customer_id = workspace.customer_id or properties.get("customerId")
        checks.extend(self._workspace_checks(properties, failure))
        checks.extend(self._transform_checks(properties.get("defaultDataCollectionRuleResourceId"), failure, deadline))
        checks.append(self._entra_diagnostics_check(workspace.resource_id, deadline))
        if customer_id:
            checks.extend(self._ingestion_checks(customer_id, deadline))
        else:
            checks.append(_check("table_ingestion", "unknown", failure or "workspace_unavailable"))
        return _summary(checks)

    @staticmethod
    def _workspace_checks(properties, failure):
        if failure:
            return [_check("daily_cap", "unknown", failure)]
        capping = properties.get("workspaceCapping") or {}
        quota = capping.get("dailyQuotaGb")
        status = str(capping.get("dataIngestionStatus") or "")
        if status.lower() in {"overquota", "subscriptionsuspended", "approachingquota"}:
            return [_check("daily_cap", "fail" if status.lower() != "approachingquota" else "warn",
                           "ingestion_" + status.lower(), daily_quota_gb=quota)]
        if isinstance(quota, (int, float)) and quota > 0:
            return [_check("daily_cap", "warn", "daily_cap_configured", daily_quota_gb=quota)]
        return [_check("daily_cap", "ok", "no_daily_cap")]

    def _transform_checks(self, dcr_id, failure, deadline):
        if failure:
            return [_check("workspace_transforms", "unknown", failure)]
        if not dcr_id:
            return [_check("workspace_transforms", "ok", "no_workspace_transform")]
        dcr, failure = self._json([
            "rest", "--method", "get", "--url",
            f"https://management.azure.com{dcr_id}?api-version={DCR_API}",
            "--output", "json", "--only-show-errors",
        ], deadline)
        if failure or not isinstance(dcr, dict):
            return [_check("workspace_transforms", "unknown", failure or "invalid_response")]
        tracked = {configured_table_name(source) for source in QUERY_PLANS}
        filtered = set()
        for flow in (dcr.get("properties") or {}).get("dataFlows") or ():
            kql = str(flow.get("transformKql") or "").lower()
            if "where" not in kql and "take" not in kql and "sample" not in kql:
                continue
            for stream in flow.get("streams") or ():
                table = str(stream).removeprefix("Microsoft-Table-")
                if table in tracked:
                    filtered.add(table)
        if filtered:
            # Row filters silently remove evidence; graph_read_coverage measures the actual effect.
            return [_check("workspace_transforms", "warn", "evidence_rows_filtered", tables=sorted(filtered))]
        return [_check("workspace_transforms", "ok", "no_evidence_filters")]

    def _entra_diagnostics_check(self, resource_id, deadline):
        payload, failure = self._json([
            "rest", "--method", "get", "--url", ENTRA_DIAGNOSTICS_URL, "--output", "json", "--only-show-errors",
        ], deadline)
        if failure or not isinstance(payload, dict):
            return _check("entra_diagnostic_settings", "unknown", failure or "invalid_response")
        enabled = set()
        for setting in payload.get("value") or ():
            properties = setting.get("properties") or {}
            if str(properties.get("workspaceId") or "").casefold() != resource_id.casefold():
                continue
            enabled.update(log.get("category") for log in properties.get("logs") or () if log.get("enabled"))
        missing = [category for category in REQUIRED_ENTRA_CATEGORIES if category not in enabled]
        if missing:
            return _check("entra_diagnostic_settings", "fail", "categories_not_exported", missing=missing)
        return _check("entra_diagnostic_settings", "ok", "required_categories_exported")

    def _ingestion_checks(self, customer_id, deadline):
        checks = []
        tables = {source: configured_table_name(source) for source in QUERY_PLANS}
        union = ", ".join(sorted(set(tables.values())))
        rows, failure = self._query(
            customer_id,
            f"union withsource=SourceTable isfuzzy=true {union}\n"
            "| where TimeGenerated >= ago(7d)\n"
            "| summarize Rows = count() by SourceTable",
            deadline,
        )
        if failure:
            checks.append(_check("table_ingestion", "unknown", failure))
        else:
            counts = {str(row.get("SourceTable")): int(row.get("Rows") or 0) for row in rows}
            silent_core = sorted(t for s, t in tables.items() if s in CORE_SOURCES and not counts.get(t))
            silent_other = sorted(t for s, t in tables.items() if s not in CORE_SOURCES and not counts.get(t))
            status = "fail" if silent_core else "warn" if silent_other else "ok"
            checks.append(_check(
                "table_ingestion", status,
                "core_sources_silent" if silent_core else "supporting_sources_silent" if silent_other else "all_sources_ingesting",
                silent_core=silent_core, silent_supporting=silent_other,
            ))
        graph = tables["MicrosoftGraphActivityLogs"]
        rows, failure = self._query(
            customer_id,
            f"{graph}\n| where TimeGenerated >= ago(7d)\n"
            "| extend UserRead = RequestMethod == 'GET' and RequestUri matches regex @'/(v1\\.0|beta)/users(/|\\?|$)'\n"
            "| summarize Rows = count(), UserReads = countif(UserRead) by RequestMethod = tostring(RequestMethod)",
            deadline,
        )
        if failure:
            checks.append(_check("graph_read_coverage", "unknown", failure))
        else:
            methods = {str(row.get("RequestMethod")).upper(): int(row.get("Rows") or 0) for row in rows}
            user_reads = sum(int(row.get("UserReads") or 0) for row in rows)
            if not methods:
                checks.append(_check("graph_read_coverage", "fail", "no_graph_activity"))
            elif not user_reads:
                # Directory reads are routine in any tenant; none at all means they are being dropped.
                checks.append(_check("graph_read_coverage", "fail", "user_reads_missing", methods=methods, user_reads=0))
            else:
                checks.append(_check("graph_read_coverage", "ok", "user_reads_present", methods=methods, user_reads=user_reads))
        return checks


def _summary(checks):
    worst = max((STATUS_ORDER[c["status"]] for c in checks), default=0)
    status = {0: "ok", 1: "unknown", 2: "degraded", 3: "failed"}[worst]
    return {"status": status, "checks": checks}


if __name__ == "__main__":
    print(json.dumps(CoverageChecker().run(), indent=2))

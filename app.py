"""Loopback-only Flask entry point for TrackBack."""

from collections import deque
import os
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, HTTPException, RequestEntityTooLarge, UnsupportedMediaType

from trackback.adapter import AzureCLIAdapter, WorkspaceConfigurationError
from trackback.correlation import correlate
from trackback.coverage import CoverageChecker
from trackback.models import InvestigationRequest

_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::([0-9]{1,5}))?", re.IGNORECASE)
_LOOPBACK = frozenset({"127.0.0.1", "::1"})


def _valid_host(value):
    match = _HOST.fullmatch(value or "")
    return bool(match and (match.group(1) is None or 1 <= int(match.group(1)) <= 65535))


def create_app(adapter=None, config=None):
    if os.getenv("HOST", "127.0.0.1") != "127.0.0.1" or os.getenv("FLASK_RUN_HOST", "127.0.0.1") != "127.0.0.1":
        raise ValueError("non_loopback_configuration")
    if os.getenv("FLASK_DEBUG", "").lower() not in {"", "0", "false", "no"}:
        raise ValueError("debug_mode_not_allowed")
    application = Flask(__name__)
    application.config.update(
        MAX_CONTENT_LENGTH=4096, DEBUG=False, PROPAGATE_EXCEPTIONS=False,
        INVESTIGATION_RATE_LIMIT=6, INVESTIGATION_RATE_WINDOW=60,
    )
    if config:
        application.config.update(config)
    if application.debug:
        raise ValueError("debug_mode_not_allowed")
    application.extensions["trackback_adapter"] = adapter if adapter is not None else AzureCLIAdapter()
    active = threading.BoundedSemaphore(1)
    rate_lock = threading.Lock()
    recent = deque()

    @application.before_request
    def local_only():
        if application.debug or not _valid_host(request.host):
            return jsonify(error="local_access_only"), 400
        if request.remote_addr is not None and request.remote_addr not in _LOOPBACK:
            return jsonify(error="local_access_only"), 400
        origin = request.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return jsonify(error="local_access_only"), 400
            if (
                parsed.scheme != request.scheme or not _valid_host(parsed.netloc)
                or parsed.netloc.lower() != request.host.lower()
                or parsed.path or parsed.query or parsed.fragment
            ):
                return jsonify(error="local_access_only"), 400
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(error="local_access_only"), 400
        if request.headers.get("Forwarded") or request.headers.get("X-Forwarded-Host"):
            return jsonify(error="local_access_only"), 400

    @application.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @application.get("/")
    def index():
        return render_template("index.html")

    @application.get("/api/health")
    def health():
        return jsonify(
            ready=bool(getattr(application.extensions["trackback_adapter"], "ready", False)),
            workspace={"name": "Configured Sentinel workspace"},
            local_only=True,
        )

    @application.post("/api/investigate")
    def investigate():
        try:
            investigation = InvestigationRequest.from_payload(request.get_json())
        except (BadRequest, UnsupportedMediaType, RequestEntityTooLarge, ValueError):
            return jsonify(error="invalid_request"), 400
        with rate_lock:
            now = time.monotonic()
            while recent and recent[0] <= now - application.config["INVESTIGATION_RATE_WINDOW"]:
                recent.popleft()
            if len(recent) >= application.config["INVESTIGATION_RATE_LIMIT"]:
                return jsonify(error="query_capacity_unavailable"), 503
            recent.append(now)
        if not active.acquire(blocking=False):
            return jsonify(error="query_capacity_unavailable"), 503
        try:
            adapter = application.extensions["trackback_adapter"]
            lead = None
            if hasattr(adapter, "investigate_with_lead"):
                outcomes, lead = adapter.investigate_with_lead(
                    investigation.target, investigation.mode, investigation.timespan,
                )
            else:
                outcomes = adapter.investigate(
                    investigation.target, investigation.mode, investigation.timespan,
                )
            result = correlate(outcomes, investigation.mode, lead=lead)
            result["timespan"] = investigation.timespan
            return jsonify(result), 503 if result["status"] == "unavailable" else 200
        finally:
            active.release()

    @application.get("/api/coverage")
    def coverage():
        if not active.acquire(blocking=False):
            return jsonify(error="query_capacity_unavailable"), 503
        try:
            return jsonify(CoverageChecker(application.extensions["trackback_adapter"]).run())
        finally:
            active.release()

    @application.errorhandler(WorkspaceConfigurationError)
    def invalid_workspace_configuration(error):
        return jsonify(error.public()), 503

    @application.errorhandler(Exception)
    def opaque_error(error):
        # The outer HTTP boundary intentionally catches unexpected failures; no
        # exception text, traceback, request values or Azure output are logged.
        if isinstance(error, HTTPException) and error.code in {404, 405}:
            return jsonify(error="not_found" if error.code == 404 else "method_not_allowed"), error.code
        error_id = uuid.uuid4().hex
        application.logger.error("investigation_internal_error error_id=%s", error_id)
        return jsonify(error="internal_error", error_id=error_id), 500

    return application


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False, use_reloader=False)

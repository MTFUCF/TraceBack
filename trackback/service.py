"""Public Flask application factory and WSGI application."""

from app import app, create_app

__all__ = ["app", "create_app"]

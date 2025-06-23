"""FastMCP Log and Reminder Server.

This example demonstrates a simple FastMCP server
that keeps a log of events and allows setting
reminders that can be retrieved when due.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Log & Reminder Server")

_logs: list[str] = []
_reminders: list[tuple[datetime, str]] = []


@mcp.tool()
def log_event(event: str) -> str:
    """Store an event in the log."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    _logs.append(f"{timestamp}: {event}")
    return "logged"


@mcp.tool()
def list_logs() -> list[str]:
    """Return all stored log entries."""
    return list(_logs)


@mcp.tool()
def add_reminder(after_seconds: int, message: str) -> str:
    """Add a reminder that becomes due after ``after_seconds``."""
    due = datetime.now() + timedelta(seconds=after_seconds)
    _reminders.append((due, message))
    return "reminder added"


@mcp.tool()
def get_due_reminders() -> list[str]:
    """Retrieve and clear reminders that are due."""
    now = datetime.now()
    due_messages = [msg for due, msg in _reminders if due <= now]
    _reminders[:] = [(d, m) for d, m in _reminders if d > now]
    return due_messages

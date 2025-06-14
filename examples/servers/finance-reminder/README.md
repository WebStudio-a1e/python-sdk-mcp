# MCP Finance Reminder Server

This example demonstrates a simple multi-agent server using the Model Context Protocol.

The server exposes two tools:

* `record_transaction` – store a financial transaction in a Google Sheet.
* `add_reminder` – save a reminder to a local SQLite database.

A background task checks the database every 30 minutes and sends due reminders
via Twilio. All credentials are loaded from environment variables.

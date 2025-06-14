import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import anyio
import click
import mcp.types as types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from mcp.server.lowlevel import Server
from twilio.rest import Client as TwilioClient


class SheetAgent:
    def __init__(self, spreadsheet_id: str, credentials_json: str) -> None:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(credentials_json),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.service = build("sheets", "v4", credentials=creds)
        self.spreadsheet_id = spreadsheet_id

    def _get_values(self) -> list[list[str]]:
        sheet = self.service.spreadsheets()
        result = (
            sheet.values()
            .get(spreadsheetId=self.spreadsheet_id, range="A2:G")
            .execute()
        )
        return result.get("values", [])

    async def record_transaction(
        self,
        fecha: str,
        tipo: str,
        categoria: str,
        monto: float,
        nota: str | None = None,
    ) -> dict:
        values = await anyio.to_thread.run_sync(self._get_values)
        last_id = len(values)
        last_balance = float(values[-1][6]) if values else 0.0
        balance = last_balance + (monto if tipo == "entrada" else -monto)
        row = [
            str(last_id + 1),
            fecha,
            tipo,
            categoria,
            f"{monto:.2f}",
            nota or "",
            f"{balance:.2f}",
        ]
        body = {"values": [row]}
        await anyio.to_thread.run_sync(
            lambda: self.service.spreadsheets()
            .values()
            .append(
                spreadsheetId=self.spreadsheet_id,
                range="A2:G",
                valueInputOption="USER_ENTERED",
                body=body,
            )
            .execute()
        )
        return {"status": "ok", "balance": balance}


class ReminderAgent:
    def __init__(self, db_path: str, twilio_client: TwilioClient | None) -> None:
        self.db_path = db_path
        self.twilio_client = twilio_client
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message TEXT NOT NULL,
                    remind_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    async def add_reminder(self, message: str, remind_at: datetime) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO reminders (message, remind_at) VALUES (?, ?)",
                (message, remind_at.isoformat()),
            )
            conn.commit()
        return {"status": "scheduled"}

    def _send_message(self, message: str) -> None:
        if not self.twilio_client:
            return
        from_number = os.environ.get("TWILIO_FROM_NUMBER")
        to_number = os.environ.get("TWILIO_TO_NUMBER")
        if from_number and to_number:
            self.twilio_client.messages.create(
                body=message, from_=from_number, to=to_number
            )

    async def run(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(minutes=1)
            rows: list[tuple[int, str, str]] = []
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, message, remind_at FROM reminders WHERE remind_at <= ?",
                    (cutoff.isoformat(),),
                ).fetchall()
                conn.execute(
                    "DELETE FROM reminders WHERE remind_at <= ?",
                    (cutoff.isoformat(),),
                )
                conn.commit()
            for _, message, _ in rows:
                self._send_message(message)
            await anyio.sleep(30 * 60)


@click.command()
@click.option("--port", default=8000, help="Port to listen on for SSE")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    help="Transport type",
)
def main(port: int, transport: str) -> int:
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    credentials_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not spreadsheet_id or not credentials_json:
        raise RuntimeError("Spreadsheet ID and credentials must be provided")

    sheet_agent = SheetAgent(spreadsheet_id, credentials_json)

    twilio_client = None
    if os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN"):
        twilio_client = TwilioClient(
            os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        )
    reminder_agent = ReminderAgent(
        os.environ.get("REMINDER_DB", "reminders.db"), twilio_client
    )

    app = Server("mcp-finance-reminder")

    @app.call_tool()
    async def record_transaction(name: str, arguments: dict) -> dict:
        if name != "record_transaction":
            raise ValueError(f"Unknown tool: {name}")
        return await sheet_agent.record_transaction(
            fecha=arguments["fecha"],
            tipo=arguments["tipo"],
            categoria=arguments["categoria"],
            monto=float(arguments["monto"]),
            nota=arguments.get("nota"),
        )

    @app.call_tool()
    async def add_reminder(name: str, arguments: dict) -> dict:
        if name != "add_reminder":
            raise ValueError(f"Unknown tool: {name}")
        remind_at = datetime.fromisoformat(arguments["remind_at"])
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        return await reminder_agent.add_reminder(arguments["message"], remind_at)

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="record_transaction",
                description="Record a transaction in a Google Sheet",
                inputSchema={
                    "type": "object",
                    "required": ["fecha", "tipo", "categoria", "monto"],
                    "properties": {
                        "fecha": {"type": "string", "description": "ISO date"},
                        "tipo": {"type": "string", "enum": ["entrada", "salida"]},
                        "categoria": {"type": "string"},
                        "monto": {"type": "number"},
                        "nota": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="add_reminder",
                description="Schedule a reminder",
                inputSchema={
                    "type": "object",
                    "required": ["message", "remind_at"],
                    "properties": {
                        "message": {"type": "string"},
                        "remind_at": {
                            "type": "string",
                            "description": "ISO datetime with timezone",
                        },
                    },
                },
            ),
        ]

    if transport == "sse":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Mount, Route

        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(reminder_agent.run)
                    await app.run(
                        streams[0], streams[1], app.create_initialization_options()
                    )
                    tg.cancel_scope.cancel()
            return Response()

        starlette_app = Starlette(
            debug=True,
            routes=[
                Route("/sse", endpoint=handle_sse, methods=["GET"]),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )

        import uvicorn

        uvicorn.run(starlette_app, host="127.0.0.1", port=port)
    else:
        from mcp.server.stdio import stdio_server

        async def arun():
            async with stdio_server() as streams:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(reminder_agent.run)
                    await app.run(
                        streams[0], streams[1], app.create_initialization_options()
                    )
                    tg.cancel_scope.cancel()

        anyio.run(arun)

    return 0

"""Exercise the patched Hermes bootstrap route against the built image."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from hermes_state import SessionDB

PROFILE = "ally-smoke"
SESSION_ID = "first-turn-smoke"
MESSAGE_ID = "8ef84387-581e-4e6f-a31d-6fbca75d95f4"
API_KEY = "allies-bootstrap-smoke-key"


class _Request:
    remote = "127.0.0.1"
    transport = None

    def __init__(self, text: str) -> None:
        self.headers = {"Authorization": f"Bearer {API_KEY}"}
        self.match_info = {"profile": PROFILE, "session_id": SESSION_ID}
        self._body = {
            "schema_version": "v1",
            "kind": "assistant_transcript_bootstrap",
            "message_id": MESSAGE_ID,
            "text": text,
        }

    async def json(self) -> dict[str, str]:
        return self._body


async def _run() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        database = SessionDB(Path(temporary) / "state.db")
        database.create_session(SESSION_ID, "api_server")
        adapter = APIServerAdapter(PlatformConfig(extra={"key": API_KEY}))
        adapter._session_db = database
        adapter._expected_api_key = lambda: API_KEY
        routes = {(method, path) for method, path, _handler in adapter._http_route_table()}
        assert ("PUT", "/api/sessions/{session_id}/bootstrap") in routes

        token = _api_request_profile.set(PROFILE)
        try:
            created = await adapter._handle_session_bootstrap(_Request("Hello from Allies"))
            duplicate = await adapter._handle_session_bootstrap(
                _Request("Hello from Allies")
            )
            conflict = await adapter._handle_session_bootstrap(
                _Request("Different greeting")
            )
        finally:
            _api_request_profile.reset(token)

        assert created.status == 201
        assert json.loads(created.text)["status"] == "created"
        assert duplicate.status == 200
        assert json.loads(duplicate.text)["status"] == "duplicate"
        assert conflict.status == 409
        rows = database.get_messages(SESSION_ID, include_inactive=True)
        assert len(rows) == 1
        assert rows[0]["role"] == "assistant"
        assert rows[0]["content"] == "Hello from Allies"
        assert rows[0]["platform_message_id"] == MESSAGE_ID
        database.close()


asyncio.run(_run())


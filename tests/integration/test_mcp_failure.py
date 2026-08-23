import asyncio
import socket

import pytest
from app.config import McpServerConfig
from app.mcp_broker import McpConnection

pytestmark = pytest.mark.integration


async def test_unavailable_mcp_server_fails_without_retaining_resources() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    connection = McpConnection(
        McpServerConfig(name="unavailable", url=f"http://127.0.0.1:{port}/mcp"),
        initial_backoff=0.01,
        max_backoff=0.02,
    )

    await asyncio.wait_for(connection.start(), timeout=2)

    assert connection.health.status == "unavailable"
    assert connection.health.last_error
    await connection.close()
    assert connection.health.status == "stopped"

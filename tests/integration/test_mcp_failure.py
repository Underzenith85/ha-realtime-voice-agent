import asyncio
import socket

import httpx
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
        McpServerConfig(name="unavailable", url=f"http://127.0.0.1:{port}/mcp")
    )

    with pytest.raises((httpx.ConnectError, ExceptionGroup)):
        await asyncio.wait_for(connection.connect(), timeout=2)

    assert connection.session is None
    assert connection._stack is None

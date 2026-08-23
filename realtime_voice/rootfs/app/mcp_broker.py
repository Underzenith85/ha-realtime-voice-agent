"""Resilient MCP discovery, namespacing, health, and invocation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from jsonschema import Draft7Validator
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from app.config import McpServerConfig

LOGGER = logging.getLogger(__name__)


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value).strip("_").lower()


def _validated_schema(schema: Any, server_name: str, tool_name: str) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"invalid input schema for MCP tool {server_name}.{tool_name}")
    try:
        Draft7Validator.check_schema(schema)
    except Exception as err:
        raise ValueError(f"invalid input schema for MCP tool {server_name}.{tool_name}") from err
    return schema


def _http_status(error: BaseException) -> int | None:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            if status := _http_status(nested):
                return status
    return None


def _safe_error(error: BaseException) -> str:
    status = _http_status(error)
    return f"HTTP {status}" if status else type(error).__name__


def _fallback_url(config: McpServerConfig) -> str | None:
    if config.name != "homeassistant" or not config.url.endswith("/api/mcp/assist"):
        return None
    return config.url[: -len("/assist")]


@dataclass(frozen=True, slots=True)
class ToolBinding:
    public_name: str
    server_name: str
    remote_name: str
    description: str
    schema: dict[str, Any]

    def realtime_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.public_name,
            "description": self.description,
            "parameters": self.schema,
        }


@dataclass(slots=True)
class McpHealth:
    name: str
    status: str = "starting"
    attempts: int = 0
    last_error: str | None = None
    fallback_active: bool = False
    schema_errors: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _Command:
    operation: str
    arguments: tuple[Any, ...]
    future: asyncio.Future[Any]


CatalogCallback = Callable[[], Awaitable[None]]
PolicyHook = Callable[[ToolBinding, dict[str, Any], str | None], bool | Awaitable[bool]]


class McpConnection:
    """Own an MCP transport in one task so its AnyIO scopes unwind safely."""

    def __init__(
        self,
        config: McpServerConfig,
        catalog_callback: CatalogCallback | None = None,
        *,
        initial_backoff: float = 1,
        max_backoff: float = 30,
        refresh_interval: float = 30,
    ) -> None:
        self.config = config
        self.health = McpHealth(config.name)
        self.tools: tuple[Any, ...] = ()
        self._catalog_callback = catalog_callback
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._refresh_interval = refresh_interval
        self._commands: asyncio.Queue[_Command] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._initial_done = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"mcp-{self.config.name}")
        await self._initial_done.wait()

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.health.status == "connected":
            future = asyncio.get_running_loop().create_future()
            await self._commands.put(_Command("stop", (), future))
        if self._task:
            await self._task
        self._task = None
        self.health.status = "stopped"

    async def refresh(self) -> None:
        await self._execute("refresh")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._execute("call", name, arguments)

    async def reconnect_now(self) -> None:
        self._wake.set()
        if self.health.status == "connected":
            await self.refresh()

    async def _execute(self, operation: str, *arguments: Any) -> Any:
        if self.health.status != "connected":
            raise ConnectionError(f"MCP server {self.config.name} is unavailable")
        future = asyncio.get_running_loop().create_future()
        await self._commands.put(_Command(operation, arguments, future))
        return await future

    async def _run(self) -> None:
        backoff = self._initial_backoff
        while not self._stop.is_set():
            self.health.status = "connecting"
            try:
                stack, session, fallback_active = await self._connect_with_fallback()
            except BaseException as err:
                if isinstance(err, asyncio.CancelledError):
                    raise
                await self._mark_unavailable()
                self.health.attempts += 1
                self.health.last_error = _safe_error(err)
                self._initial_done.set()
                LOGGER.warning(
                    "MCP server %s unavailable (%s); retrying in %.1fs",
                    self.config.name,
                    self.health.last_error,
                    backoff,
                )
                await self._wait_to_retry(backoff)
                backoff = min(backoff * 2, self._max_backoff)
                continue

            async with stack:
                self.health.status = "connected"
                self.health.attempts = 0
                self.health.last_error = None
                self.health.fallback_active = fallback_active
                backoff = self._initial_backoff
                try:
                    await self._replace_tools((await session.list_tools()).tools)
                    self._initial_done.set()
                    await self._serve(session)
                except BaseException as err:
                    if isinstance(err, asyncio.CancelledError):
                        raise
                    await self._mark_unavailable()
                    self.health.attempts += 1
                    self.health.last_error = _safe_error(err)
                    self._initial_done.set()
                    self._fail_pending(err)
                    LOGGER.warning(
                        "MCP server %s disconnected (%s)",
                        self.config.name,
                        self.health.last_error,
                    )
            if not self._stop.is_set():
                await self._wait_to_retry(backoff)
                backoff = min(backoff * 2, self._max_backoff)
        self._fail_pending(ConnectionError(f"MCP server {self.config.name} stopped"))

    async def _connect_with_fallback(
        self,
    ) -> tuple[AsyncExitStack, ClientSession, bool]:
        try:
            stack, session = await self._connect(self.config.url)
            return stack, session, False
        except BaseException as err:
            fallback = _fallback_url(self.config)
            if _http_status(err) != 404 or fallback is None:
                raise
            LOGGER.info("Home Assistant Assist MCP endpoint returned 404; using MCP endpoint")
            stack, session = await self._connect(fallback)
            return stack, session, True

    async def _connect(self, url: str) -> tuple[AsyncExitStack, ClientSession]:
        stack = AsyncExitStack()
        try:
            if self.config.transport == "sse":
                streams = await stack.enter_async_context(
                    sse_client(url, headers=self.config.headers)
                )
            else:
                streams = await stack.enter_async_context(
                    streamablehttp_client(url, headers=self.config.headers)
                )
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
            return stack, session
        except BaseException:
            await stack.aclose()
            raise

    async def _serve(self, session: ClientSession) -> None:
        while not self._stop.is_set():
            try:
                command = await asyncio.wait_for(
                    self._commands.get(), timeout=self._refresh_interval
                )
            except TimeoutError:
                await self._replace_tools((await session.list_tools()).tools)
                continue
            try:
                if command.operation == "stop":
                    if not command.future.done():
                        command.future.set_result(None)
                    return
                if command.operation == "refresh":
                    await self._replace_tools((await session.list_tools()).tools)
                    if not command.future.done():
                        command.future.set_result(None)
                else:
                    name, arguments = command.arguments
                    result = await session.call_tool(name, arguments)
                    if not command.future.done():
                        command.future.set_result(result)
            except BaseException as err:
                if not command.future.done():
                    command.future.set_exception(err)
                raise

    async def _replace_tools(self, tools: list[Any]) -> None:
        updated = tuple(tools)
        if updated == self.tools:
            return
        self.tools = updated
        if self._catalog_callback:
            await self._catalog_callback()

    async def _mark_unavailable(self) -> None:
        self.health.status = "unavailable"
        if self.tools:
            self.tools = ()
            if self._catalog_callback:
                await self._catalog_callback()

    async def _wait_to_retry(self, delay: float) -> None:
        self._wake.clear()
        wake = asyncio.create_task(self._wake.wait())
        stop = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {wake, stop}, timeout=delay, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    def _fail_pending(self, error: BaseException) -> None:
        while not self._commands.empty():
            command = self._commands.get_nowait()
            if not command.future.done():
                command.future.set_exception(error)


class McpBroker:
    def __init__(
        self,
        configs: tuple[McpServerConfig, ...],
        output_limit: int = 16_384,
        shared_concurrency: int = 16,
        policy_hook: PolicyHook | None = None,
    ) -> None:
        self.bindings: dict[str, ToolBinding] = {}
        self.catalog_version = 0
        self.output_limit = output_limit
        self._call_limit = asyncio.Semaphore(shared_concurrency)
        self._policy_hook = policy_hook
        self._catalog_lock = asyncio.Lock()
        self.connections = {
            config.name: McpConnection(config, self._rebuild_catalog) for config in configs
        }

    async def start(self) -> None:
        await asyncio.gather(*(connection.start() for connection in self.connections.values()))
        homeassistant = self.connections.get("homeassistant")
        if homeassistant and homeassistant.health.status != "connected":
            await self.close()
            raise ConnectionError("Home Assistant MCP server is unavailable")
        await self._rebuild_catalog()

    async def close(self) -> None:
        await asyncio.gather(
            *(connection.close() for connection in self.connections.values()),
            return_exceptions=True,
        )

    async def refresh(self) -> None:
        await asyncio.gather(
            *(connection.reconnect_now() for connection in self.connections.values()),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(connection.refresh() for connection in self.connections.values()),
            return_exceptions=True,
        )
        await self._rebuild_catalog()

    async def _rebuild_catalog(self) -> None:
        async with self._catalog_lock:
            bindings: dict[str, ToolBinding] = {}
            candidates: list[tuple[str, Any, dict[str, Any]]] = []
            for name, connection in self.connections.items():
                connection.health.schema_errors = []
                if connection.health.status != "connected":
                    continue
                for tool in connection.tools:
                    allowed = connection.config.allowed_tools
                    if (
                        name != "homeassistant"
                        and not connection.config.expose_all_tools
                        and not allowed
                    ):
                        continue
                    if allowed and tool.name not in allowed:
                        continue
                    try:
                        schema = _validated_schema(
                            tool.inputSchema or {"type": "object", "properties": {}},
                            name,
                            tool.name,
                        )
                    except ValueError as err:
                        connection.health.schema_errors.append(str(err))
                        continue
                    candidates.append((name, tool, schema))
            for name, tool, schema in sorted(candidates, key=lambda item: (item[0], item[1].name)):
                public_name = self._unique_name(name, tool.name, bindings)
                bindings[public_name] = ToolBinding(
                    public_name=public_name,
                    server_name=name,
                    remote_name=tool.name,
                    description=tool.description or f"Tool from {name}",
                    schema=schema,
                )
            if bindings != self.bindings:
                self.bindings = bindings
                self.catalog_version += 1

    @staticmethod
    def _unique_name(server_name: str, tool_name: str, bindings: dict[str, ToolBinding]) -> str:
        raw = f"mcp_{_slug(server_name)}_{_slug(tool_name)}"
        candidate = raw[:64]
        if candidate not in bindings:
            return candidate
        digest = hashlib.sha256(f"{server_name}\0{tool_name}".encode()).hexdigest()[:8]
        candidate = f"{raw[:55]}_{digest}"
        if candidate in bindings:
            raise ValueError(f"duplicate MCP tool identity: {server_name}.{tool_name}")
        return candidate

    def snapshot(self) -> tuple[int, dict[str, ToolBinding]]:
        return self.catalog_version, self.bindings.copy()

    async def call_binding(
        self,
        binding: ToolBinding,
        arguments: dict[str, Any],
        *,
        client_id: str | None = None,
    ) -> str:
        started = time.monotonic()
        argument_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        outcome = "error"
        try:
            if not Draft7Validator(binding.schema).is_valid(arguments):
                outcome = "invalid_arguments"
                raise ValueError("tool arguments do not match the advertised schema")
            if self._policy_hook:
                allowed = self._policy_hook(binding, arguments, client_id)
                if inspect.isawaitable(allowed):
                    allowed = await allowed
                if not allowed:
                    outcome = "policy_denied"
                    raise PermissionError("tool call denied by policy")
            connection = self.connections[binding.server_name]
            async with self._call_limit:
                result = await asyncio.wait_for(
                    connection.call_tool(binding.remote_name, arguments),
                    timeout=connection.config.call_timeout_seconds,
                )
            payload = json.dumps(result.model_dump(mode="json"), separators=(",", ":"))
            if len(payload.encode()) > self.output_limit:
                prefix = payload.encode()[: max(0, self.output_limit - 80)].decode(errors="ignore")
                payload = json.dumps(
                    {"truncated": True, "content_prefix": prefix}, separators=(",", ":")
                )
            outcome = "success"
            return payload
        finally:
            LOGGER.info(
                "MCP tool audit %s",
                json.dumps(
                    {
                        "tool": binding.public_name,
                        "server": binding.server_name,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                        "outcome": outcome,
                        "client_id": client_id,
                        "argument_sha256": argument_hash,
                    },
                    separators=(",", ":"),
                ),
            )

    async def call(self, public_name: str, arguments: dict[str, Any]) -> str:
        binding = self.bindings.get(public_name)
        if binding is None:
            return json.dumps({"error": "tool_not_available"})
        return await self.call_binding(binding, arguments)

    def realtime_tools(self) -> list[dict[str, Any]]:
        return [binding.realtime_definition() for binding in self.bindings.values()]

    def status(self) -> dict[str, Any]:
        return {
            "catalog_version": self.catalog_version,
            "tool_count": len(self.bindings),
            "servers": [connection.health.public() for connection in self.connections.values()],
        }

    async def reconcile_managed(self, configs: tuple[McpServerConfig, ...]) -> None:
        """Add/remove HA-managed MCP APIs after OAuth entries change."""
        desired = {config.name: config for config in configs}
        existing = {
            name: connection
            for name, connection in self.connections.items()
            if connection.config.managed_by_home_assistant
        }
        for name in existing.keys() - desired.keys():
            connection = self.connections.pop(name)
            await connection.close()
        for name in desired.keys() - existing.keys():
            connection = McpConnection(desired[name], self._rebuild_catalog)
            self.connections[name] = connection
            await connection.start()
        await self._rebuild_catalog()

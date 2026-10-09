# Copyright (c) 2026 Huawei Technologies Co.,Ltd.
#
# openGauss is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""ServerConnection - lifecycle of a single MCP server connection."""

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.session_group import ClientSessionParameters
from mcp_types import CallToolResult

from ._component_registry import ComponentRegistry, _ComponentNames
from ._server_status import HealthStatus, ServerStatus, ServerStatusInfo
from ._session_factory import SessionContext, SessionFactory, stdio_errlog_path
from .exceptions import (
    MCPServerConnectionError,
    MCPServerDisconnectedError,
    MCPServerUnreachableError,
    is_connection_closed_error,
)
from .mcp_config import HttpServerConfig, StdioServerConfig, TransportType

logger = logging.getLogger(__name__)

PING_TTL_SECONDS = 5.0
PING_TIMEOUT_SECONDS = 5.0
DISCONNECT_TIMEOUT_SECONDS = 10.0


class ServerConnection:
    """Lifecycle domain object of a single MCP server connection."""

    def __init__(
        self,
        server_config_name: str,
        config: StdioServerConfig | HttpServerConfig,
        registry: ComponentRegistry,
    ) -> None:
        self.server_config_name = server_config_name
        self.config = config
        self._registry = registry
        self.status: ServerStatus = ServerStatus.NOT_CONNECTED
        self.failure_message: str = ""
        self.desired_connected: bool = False
        self.unreachable_count: int = 0
        self._session: ClientSession | None = None
        self._session_context: SessionContext | None = None
        self._component_names: _ComponentNames | None = None
        self._lifecycle_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._reconnect_fail_count: int = 0
        self._last_alive_confirm_time: float | None = None

    @property
    def status_info(self) -> ServerStatusInfo:
        """State snapshot."""
        return ServerStatusInfo(
            server_config_name=self.server_config_name,
            status=self.status,
            transport=self.transport,
            message=self.failure_message,
        )

    @property
    def is_alive(self) -> bool:
        """Whether the connection is alive."""
        return self._session is not None

    @property
    def is_connecting(self) -> bool:
        """Whether the initial connection is in progress."""
        return self.status is ServerStatus.CONNECTING

    @property
    def is_reconnecting(self) -> bool:
        """Whether an auto-reconnect is in flight."""
        return self._reconnect_task is not None and not self._reconnect_task.done()

    @property
    def transport(self) -> TransportType:
        """Transport type."""
        return self.config.transport

    async def connect(self, read_timeout_seconds: float | None = None) -> None:
        """Establish the connection.

        Raises:
            MCPServerConnectionError: Connection setup failed
        """
        self.desired_connected = True
        await self._attempt_connect(ServerStatus.CONNECTING, read_timeout_seconds)

    def reconnect(self) -> None:
        """Dispatch a single auto-reconnect (non-blocking, idempotent)."""
        if not self.desired_connected or self.is_alive:
            return
        if self.is_connecting or self.is_reconnecting:
            return
        logger.debug("Server '%s' has no live session; dispatching auto-reconnect", self.server_config_name)
        self._reconnect_task = asyncio.create_task(self._run_reconnect())

    async def _run_reconnect(self) -> None:
        """Single auto-reconnect execution body."""
        try:
            if not self.desired_connected or self.is_alive:
                return
            await self._attempt_connect(ServerStatus.RECONNECTING, None)
            if self.status is ServerStatus.CONNECTED:
                self._reconnect_fail_count = 0
                logger.info("Server '%s' auto-reconnected successfully", self.server_config_name)
        except Exception as exc:
            self._reconnect_fail_count += 1
            detail = str(exc) or repr(exc)
            if self._reconnect_fail_count == 1 or self._reconnect_fail_count % 10 == 0:
                logger.warning(
                    "Server '%s' auto-reconnect failed (%d consecutive failures): %s (retrying next inspection round)",
                    self.server_config_name, self._reconnect_fail_count, detail,
                )
            else:
                logger.debug(
                    "Server '%s' auto-reconnect failed (consecutive failure #%d): %s",
                    self.server_config_name, self._reconnect_fail_count, detail,
                )
        finally:
            self._reconnect_task = None

    async def disconnect(self, *, keep_desired_connected: bool = False) -> None:
        """Disconnect this connection (idempotent).

        Args:
            keep_desired_connected: True keeps the desired-connected flag.
        """
        if not keep_desired_connected:
            self.desired_connected = False
        was_alive = self.is_alive
        reconnect_task = self._reconnect_task
        if reconnect_task is not None and not reconnect_task.done():
            reconnect_task.cancel()
            await self._await_finalization(reconnect_task, "canceling in-flight reconnect")
        lifecycle_task = self._lifecycle_task
        if lifecycle_task is not None and not lifecycle_task.done():
            lifecycle_task.cancel()
            await self._await_finalization(lifecycle_task, "disconnect cleanup")
        elif self._session_context is not None:
            await self._await_finalization(self._teardown_session(), "disconnect cleanup")
        if was_alive:
            self.status = ServerStatus.DISCONNECTED

    async def _await_finalization(
        self, target: asyncio.Task[Any] | Coroutine[Any, Any, Any], action: str
    ) -> None:
        """Wait for a cancelled task to finish within a time limit."""
        try:
            await asyncio.wait_for(target, DISCONNECT_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning(
                "Server '%s' %s timed out (%.0fs); gave up waiting (the background task may linger)",
                self.server_config_name, action, DISCONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            pass
        except BaseException:
            logger.debug(
                "Server '%s' %s raised an error", self.server_config_name, action, exc_info=True
            )

    async def _attempt_connect(
        self, in_progress_status: ServerStatus, read_timeout_seconds: float | None
    ) -> None:
        """Common connection setup path.

        Args:
            in_progress_status: Status registered during connection setup
            read_timeout_seconds: Read timeout in seconds
        """
        if self.is_alive or self.status in (
            ServerStatus.CONNECTING,
            ServerStatus.RECONNECTING,
        ):
            return
        self.status = in_progress_status
        self.failure_message = ""

        ready_future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        lifecycle_task = asyncio.create_task(
            self._run_lifecycle(read_timeout_seconds, ready_future)
        )
        self._lifecycle_task = lifecycle_task
        ready_future.add_done_callback(
            lambda fut: lifecycle_task.cancel() if fut.cancelled() else None
        )
        try:
            await ready_future
        except BaseException as exc:
            message = str(exc) or repr(exc)
            self._mark_connect_failed(message)
            raise MCPServerConnectionError(message) from exc
        self.status = ServerStatus.CONNECTED
        self.unreachable_count = 0
        logger.info("Connected to MCP server (%s): %s", self.transport, self.server_config_name)

    async def _run_lifecycle(
        self, read_timeout_seconds: float | None, ready_future: asyncio.Future[None]
    ) -> None:
        """Complete lifecycle task of a single connection (session work must run in this task)."""
        session_context: SessionContext | None = None
        try:
            session_context = await self._establish_session(read_timeout_seconds)
            if ready_future.cancelled():
                raise asyncio.CancelledError()
            if not ready_future.done():
                ready_future.set_result(None)
            await asyncio.Event().wait()
        except BaseException as exc:
            if not ready_future.done():
                ready_future.set_exception(exc)
        finally:
            if session_context is not None and self._session_context is not None:
                logger.debug("Disconnecting session of server '%s'", self.server_config_name)
                await self._teardown_session()

    async def _establish_session(
        self, read_timeout_seconds: float | None
    ) -> SessionContext:
        """Build the transport, initialize the session, and register components."""
        errlog_path = (
            stdio_errlog_path(self.server_config_name)
            if isinstance(self.config, StdioServerConfig)
            else None
        )
        session_context = await SessionFactory.create_session(
            self.config.to_server_params(),
            ClientSessionParameters(read_timeout_seconds=read_timeout_seconds),
            errlog_path=errlog_path,
        )
        session = session_context.session
        try:
            await session.initialize()
        except BaseException:
            await session_context.close()
            raise

        self._session = session
        self._session_context = session_context
        try:
            await self._aggregate_components(session)
        except BaseException:
            await self._teardown_session()
            raise
        return session_context

    async def _teardown_session(self) -> None:
        """Tear down this connection."""
        self._session = None
        self._last_alive_confirm_time = None
        session_context = self._session_context
        self._session_context = None
        record = self._component_names
        self._component_names = None
        if session_context is not None:
            if record is not None:
                self._registry.unregister(record)
            await session_context.close()

    async def _aggregate_components(self, session: ClientSession) -> None:
        """Fetch prompts / resources / tools and register them."""
        results = await asyncio.gather(
            self._fetch_component_list(session, "list_prompts", "prompts"),
            self._fetch_component_list(session, "list_resources", "resources"),
            self._fetch_component_list(session, "list_tools", "tools"),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        prompts, resources, tools = results
        self._component_names = self._registry.register(
            self, self.server_config_name, prompts, resources, tools
        )

    async def _fetch_component_list(
        self, session: ClientSession, rpc_name: str, component_kind: str
    ) -> list:
        """Fetch one component category."""
        try:
            result = await getattr(session, rpc_name)()
        except Exception as exc:
            if is_connection_closed_error(exc):
                raise
            logger.warning(
                "Failed to fetch %s from server '%s': %s",
                self.server_config_name, component_kind, exc,
            )
            return []
        return getattr(result, component_kind)

    async def call_tool(
        self, session_tool_name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult:
        """Call a tool on this connection (pings to check aliveness before the call).

        Args:
            session_tool_name: Raw tool name within the session
            arguments: Tool arguments

        Raises:
            RuntimeError: No live session on this connection
            MCPServerUnreachableError: Transiently unreachable; retryable
            MCPServerDisconnectedError: Connection closed; awaiting auto-reconnect
        """
        session = self._session
        if session is None:
            raise RuntimeError(f"Server '{self.server_config_name}' is not connected")
        await self._ping_before_call(session_tool_name, session)
        try:
            result = await session.call_tool(session_tool_name, arguments=arguments)
        except Exception as exc:
            if not is_connection_closed_error(exc):
                self._last_alive_confirm_time = time.monotonic()
                raise
            logger.warning(
                "Detected a disconnected connection while calling tool '%s'; cleaning up the session",
                session_tool_name,
            )
            await self.disconnect(keep_desired_connected=True)
            raise MCPServerDisconnectedError(session_tool_name, str(exc)) from exc
        self._last_alive_confirm_time = time.monotonic()
        return result

    async def _ping_before_call(
        self, tool_name: str, session: ClientSession
    ) -> None:
        """Aliveness check before tool calls (skipped within TTL)."""
        if (
            self._last_alive_confirm_time is not None
            and (time.monotonic() - self._last_alive_confirm_time) < PING_TTL_SECONDS
        ):
            return
        try:
            await asyncio.wait_for(session.send_ping(), PING_TIMEOUT_SECONDS)
        except Exception as exc:
            if is_connection_closed_error(exc):
                logger.warning(
                    "Detected a disconnected connection before calling tool '%s'; cleaning up the session", tool_name
                )
                await self.disconnect(keep_desired_connected=True)
                raise MCPServerDisconnectedError(tool_name, str(exc)) from exc
            raise MCPServerUnreachableError(str(exc)) from exc
        self._last_alive_confirm_time = time.monotonic()

    async def check_health(self, *, ping_timeout: float) -> HealthStatus:
        """Health inspection check: pings and returns a verdict."""
        session = self._session
        if session is None:
            return HealthStatus.HEALTHY
        try:
            await asyncio.wait_for(session.send_ping(), ping_timeout)
        except asyncio.CancelledError:
            return HealthStatus.UNREACHABLE
        except asyncio.TimeoutError:
            return HealthStatus.UNREACHABLE
        except Exception as exc:
            if is_connection_closed_error(exc):
                logger.warning(
                    "Health inspection detected a disconnected connection of server '%s'; cleaning up the session",
                    self.server_config_name,
                )
                await self.disconnect(keep_desired_connected=True)
                return HealthStatus.DEAD
            return HealthStatus.UNREACHABLE
        self._last_alive_confirm_time = time.monotonic()
        return HealthStatus.HEALTHY

    def _mark_connect_failed(self, message: str) -> None:
        """Mark the most recent connection attempt as failed."""
        self.status = ServerStatus.CONNECT_FAILED
        self.failure_message = message

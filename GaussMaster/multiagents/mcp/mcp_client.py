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

"""Session layer facade (MCPClient)."""

import asyncio
import json
import logging
import threading
from typing import Any, Mapping

from mcp_types import CallToolResult, TextContent, Tool

from ._component_registry import ComponentRegistry
from ._health_supervisor import HealthSupervisor
from ._server_connection import ServerConnection
from ._server_status import ServerStatus, ServerStatusInfo
from .mcp_config import HttpServerConfig, StdioServerConfig

logger = logging.getLogger(__name__)


def _format_tool_result(result: CallToolResult) -> str:
    """Convert an MCP tool call result into text content."""
    parts: list[str] = []
    for content in result.content:
        if isinstance(content, TextContent) and content.text:
            parts.append(content.text)

    if result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))

    text = "\n".join(parts)
    if result.is_error:
        text = f"[Tool execution error]\n{text}" if text else "[Tool execution error]"
    return text


class MCPClient:
    """MCP connection aggregation facade (concurrency-safe singleton)."""

    _instance: "MCPClient | None" = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "MCPClient":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._connections: dict[str, ServerConnection] = {}
        self._registry = ComponentRegistry()
        self._supervisor = HealthSupervisor(lambda: list(self._connections.values()))

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Return a read-only view of the aggregated tool dict."""
        return self._registry.tools

    def get_server_status(self) -> dict[str, ServerStatusInfo]:
        """Return status snapshots of all servers."""
        return {
            name: connection.status_info
            for name, connection in self._connections.items()
        }

    async def connect_stdio(
        self,
        server_config_name: str,
        config: StdioServerConfig,
        read_timeout_seconds: float | None = None,
    ) -> None:
        """Connect to an MCP server via the stdio transport.

        Args:
            server_config_name:   Server name configured in the MCP config file
            config:               stdio server configuration
            read_timeout_seconds: Read timeout in seconds

        Raises:
            ValueError: Empty name or duplicate server name
            MCPServerConnectionError: Connection setup failed
        """
        connection = self._prepare_connection(server_config_name, config)
        await connection.connect(read_timeout_seconds=read_timeout_seconds)

    async def connect_http(
        self,
        server_config_name: str,
        config: HttpServerConfig,
        read_timeout_seconds: float | None = None,
    ) -> None:
        """Connect to a remote MCP server via the Streamable HTTP transport.

        Args:
            server_config_name:   Server name configured in the MCP config file
            config:               HTTP server configuration
            read_timeout_seconds: Read timeout in seconds

        Raises:
            ValueError: Empty name or duplicate server name
            MCPServerConnectionError: Connection setup failed
        """
        connection = self._prepare_connection(server_config_name, config)
        await connection.connect(read_timeout_seconds=read_timeout_seconds)

    def _prepare_connection(
        self, server_config_name: str, config: StdioServerConfig | HttpServerConfig
    ) -> ServerConnection:
        """Validate the name, check duplicates, and create or reuse the connection."""
        if not server_config_name or not server_config_name.strip():
            raise ValueError("server_config_name must not be empty")
        existing = self._connections.get(server_config_name)
        if existing is not None and (
            existing.is_alive or existing.is_connecting or existing.is_reconnecting
        ):
            raise ValueError(f"Server '{server_config_name}' already exists; duplicate connection is not allowed")
        if existing is None:
            connection = ServerConnection(server_config_name, config, self._registry)
            self._connections[server_config_name] = connection
        else:
            connection = existing
            connection.config = config
        self._supervisor.start()
        return connection

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on an MCP server (routed automatically to the right server).

        Args:
            tool_name: Tool name (prefixed with the config name)
            arguments: Tool arguments

        Returns:
            Text content of the tool execution result

        Raises:
            RuntimeError: No MCP server connected or tool not found
            MCPServerUnreachableError: Transiently unreachable; retryable
            MCPServerDisconnectedError: Connection closed; awaiting auto-reconnect
        """
        if not self._connections:
            raise RuntimeError("No MCP server is connected")
        resolved = self._registry.resolve_tool(tool_name)
        if resolved is None:
            raise RuntimeError(f"No session found for tool '{tool_name}'")
        connection, session_tool_name = resolved
        result = await connection.call_tool(session_tool_name, arguments)
        return _format_tool_result(result)

    async def disconnect_all(self) -> None:
        """Disconnect all MCP server connections and clear the collection."""
        if self._connections:
            await asyncio.gather(
                *(
                    connection.disconnect()
                    for connection in list(self._connections.values())
                ),
                return_exceptions=True,
            )
        self._connections.clear()

    async def close(self) -> None:
        """Close all connections and release all resources."""
        await self._supervisor.stop()
        await self.disconnect_all()

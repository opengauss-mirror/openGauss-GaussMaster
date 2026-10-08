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

"""MCP custom exception definitions and disconnected-connection detection."""

import anyio
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED


class MCPServerError(RuntimeError):
    """Common base class for typed MCP exceptions."""


class MCPServerDisconnectedError(MCPServerError):
    """Typed exception raised when the MCP server connection has been closed."""

    def __init__(self, tool_name: str, reason: str) -> None:
        self.tool_name = tool_name
        self.reason = reason
        super().__init__(
            f"Detected a disconnected connection while calling MCP tool '{tool_name}': {reason}"
        )


class MCPServerUnreachableError(MCPServerError):
    """Typed exception raised when the MCP server is transiently unreachable (does not trigger session cleanup)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"MCP server ping check failed (connection not cleaned up; retryable): {reason}"
        )


class MCPServerConnectionError(MCPServerError):
    """Typed exception raised when connecting to the MCP server fails."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Failed to connect to the MCP server: {reason}")


def is_connection_closed_error(exc: Exception) -> bool:
    """Determine whether the exception indicates the underlying connection has been closed."""
    if isinstance(exc, MCPError) and exc.code == CONNECTION_CLOSED:
        return True
    return isinstance(
        exc,
        (
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
            anyio.EndOfStream,
            EOFError,
            ConnectionError,
        ),
    )

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

"""MCP session layer package initialization."""

from ._server_status import ServerStatus, ServerStatusInfo
from .exceptions import (
    MCPServerConnectionError,
    MCPServerDisconnectedError,
    MCPServerError,
    MCPServerUnreachableError,
)
from .mcp_client import MCPClient
from .mcp_config import (
    HttpServerConfig,
    MCPServersConfig,
    MCPServersConfigError,
    StdioServerConfig,
    TransportType,
)
from .mcp_manager import MCPManager

__all__ = [
    "MCPClient",
    "MCPManager",
    "ServerStatus",
    "ServerStatusInfo",
    "MCPServersConfig",
    "MCPServersConfigError",
    "StdioServerConfig",
    "HttpServerConfig",
    "TransportType",
    "MCPServerError",
    "MCPServerConnectionError",
    "MCPServerDisconnectedError",
    "MCPServerUnreachableError",
]

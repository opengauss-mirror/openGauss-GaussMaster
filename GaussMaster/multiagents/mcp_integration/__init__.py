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

"""MCP business adaptation layer package - the only entry point wiring the mcp
session layer into GaussMaster business orchestration."""

from ..mcp._server_status import ServerStatus
from .mcp_tool_adapter import MCPToolAdapter
from .tool_view import ToolView, all_tools
from .integration import (
    get_mcp_server_status,
    load_and_connect_mcp_servers,
    reload_mcp_servers,
    serialize_server_status,
)

__all__ = [
    "MCPToolAdapter",
    "ToolView",
    "all_tools",
    "load_and_connect_mcp_servers",
    "reload_mcp_servers",
    "get_mcp_server_status",
    "serialize_server_status",
    "ServerStatus",
]

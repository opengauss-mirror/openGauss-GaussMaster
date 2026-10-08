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

"""Server status view - ServerStatus / HealthStatus enums and the ServerStatusInfo snapshot model."""

from dataclasses import dataclass
from enum import Enum

from .mcp_config import TransportType


class ServerStatus(str, Enum):
    """Lifecycle states of an MCP server."""

    NOT_CONNECTED = "not_connected"
    CONNECTING = "connecting"
    RECONNECTING = "reconnecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONNECT_FAILED = "connect_failed"

    @property
    def label(self) -> str:
        """Human-readable label."""
        return {
            ServerStatus.NOT_CONNECTED: "Not connected",
            ServerStatus.CONNECTING: "Connecting",
            ServerStatus.RECONNECTING: "Reconnecting",
            ServerStatus.CONNECTED: "Connected",
            ServerStatus.DISCONNECTED: "Disconnected unexpectedly",
            ServerStatus.CONNECT_FAILED: "Connection failed",
        }[self]

    @property
    def description(self) -> str:
        """Detailed status description."""
        return {
            ServerStatus.NOT_CONNECTED: "Registered in configuration but never attempted to connect",
            ServerStatus.CONNECTING: "Connection attempt in progress",
            ServerStatus.RECONNECTING: "Auto-reconnect in progress after an unexpected disconnection",
            ServerStatus.CONNECTED: "Connected with a live session",
            ServerStatus.DISCONNECTED: "Connection was alive but has been disconnected and its session cleaned up "
            "(self-cleanup on call-time disconnect or forced disconnect by inspection threshold); "
            "the revival scan will auto-reconnect",
            ServerStatus.CONNECT_FAILED: "The most recent connection attempt failed",
        }[self]


class HealthStatus(str, Enum):
    """Verdict of a single health inspection check."""

    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    DEAD = "dead"


@dataclass
class ServerStatusInfo:
    """Status information of a single MCP server.

    Attributes:
        server_config_name: Server name from the configuration
        status:             Lifecycle status
        transport:          Transport type (TransportType enum)
        message:            Failure reason (CONNECT_FAILED only)
    """

    server_config_name: str
    status: ServerStatus
    transport: TransportType
    message: str = ""

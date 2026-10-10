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

"""MCP business integration layer - connection orchestration and status queries."""

import logging
from pathlib import Path

from GaussMaster.constants import MCP_CONFIG_FILE_NAME

from ..mcp.mcp_client import MCPClient
from ..mcp.mcp_config import MCPServersConfig, MCPServersConfigError
from ..mcp.mcp_manager import MCPManager
from ..mcp._server_status import ServerStatusInfo

logger = logging.getLogger(__name__)


def _config_path(confpath: str | Path) -> Path:
    return Path(confpath) / MCP_CONFIG_FILE_NAME


def get_mcp_server_status() -> dict[str, ServerStatusInfo]:
    """Query connection status snapshots of all MCP servers.

    Returns:
        dict[server_name, ServerStatusInfo]: Status snapshot of each server
    """
    return MCPClient().get_server_status()


def serialize_server_status(results: dict[str, ServerStatusInfo]) -> dict[str, dict]:
    """Serialize status snapshots into an API-returnable structure.

    Args:
        results: Status snapshots

    Returns:
        dict[server_name, dict]: status / status_label / transport / message
    """
    return {
        name: {
            "status": info.status.value,
            "status_label": info.status.label,
            "transport": info.transport.value,
            "message": info.message,
        }
        for name, info in results.items()
    }


async def load_and_connect_mcp_servers(confpath: str) -> dict[str, ServerStatusInfo]:
    """Load the configuration from mcp_servers.json and connect all MCP servers.

    Args:
        confpath: Configuration directory path

    Returns:
        dict[server_name, ServerStatusInfo]: Status snapshot of each server;
        an empty dict is returned when config loading fails
    """
    path = _config_path(confpath)
    try:
        MCPServersConfig.from_file(path)
    except MCPServersConfigError as exc:
        message = f"Failed to load MCP configuration; MCP initialization skipped: {exc}"
        logger.error(message)
        import GaussMaster.common.utils
        from GaussMaster.common.utils.cli import write_to_terminal
        write_to_terminal(message, level='error', color='red')
        return {}

    await MCPManager(path).connect_all()
    logger.info("MCP connections completed; %d tools aggregated", len(MCPClient().tools))
    return MCPClient().get_server_status()


async def reload_mcp_servers(confpath: str) -> dict[str, ServerStatusInfo]:
    """Hot-reload the MCP configuration.

    Args:
        confpath: Configuration directory path

    Returns:
        dict[server_name, ServerStatusInfo]: Status snapshot after reconnection
    """
    await MCPManager(_config_path(confpath)).reload()
    logger.info("Hot reload completed; %d tools aggregated", len(MCPClient().tools))
    return MCPClient().get_server_status()

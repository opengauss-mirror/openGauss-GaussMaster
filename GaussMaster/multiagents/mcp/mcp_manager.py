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

"""MCP Manager - config loading, connection management, hot reload."""

import asyncio
import logging
from pathlib import Path

from .mcp_client import MCPClient
from .mcp_config import (
    HttpServerConfig,
    MCPServersConfig,
    MCPServersConfigError,
    StdioServerConfig,
)

logger = logging.getLogger(__name__)


class MCPManager:
    """MCP connection manager."""

    def __init__(self, config_path: str | Path) -> None:
        self._config_path: Path = Path(config_path)

    async def connect_all(self) -> None:
        """Load the configuration and connect all MCP servers."""
        config = self._load_config()
        if config is None:
            return
        if config.is_empty:
            logger.warning("No MCP server defined in the config file")
            return
        await self._connect_from_config(config)

    async def reload(self) -> None:
        """Hot-reload the MCP configuration."""
        config = self._load_config()
        if config is None:
            logger.error("Hot reload aborted: failed to load MCP configuration, keeping existing connections")
            return

        if not await self._disconnect_client():
            logger.error(
                "Hot reload aborted: failed to disconnect old connections; "
                "lingering sessions would conflict with reconnection")
            return

        if config.is_empty:
            logger.warning(
                "No MCP server defined in the reloaded config file; "
                "all old connections have been disconnected")
            return
        await self._connect_from_config(config)

    async def _connect_from_config(self, config: MCPServersConfig) -> None:
        """Connect all MCP servers concurrently from the configuration."""
        await asyncio.gather(
            *(self._connect_one(name, cfg) for name, cfg in config.servers.items())
        )

    async def _connect_one(
        self, name: str, cfg: StdioServerConfig | HttpServerConfig
    ) -> None:
        """Connect a single MCP server with exception isolation."""
        client = MCPClient()
        if isinstance(cfg, StdioServerConfig):
            transport, coro = cfg.transport, client.connect_stdio(server_config_name=name, config=cfg)
        else:
            transport, coro = cfg.transport, client.connect_http(server_config_name=name, config=cfg)
        try:
            await coro
        except BaseException as e:
            message = str(e) or repr(e)
            logger.warning("Failed to connect to %s server '%s': %s", transport, name, message)
            logger.debug("Failed to connect to %s server '%s'", transport, name, exc_info=True)

    def _load_config(self) -> MCPServersConfig | None:
        """Load the MCP server config file; returns None on failure."""
        try:
            return MCPServersConfig.from_file(self._config_path)
        except MCPServersConfigError as e:
            logger.error("Failed to load MCP configuration: %s", e, stack_info=True)
            return None

    async def _disconnect_client(self) -> bool:
        """Disconnect all connections; returns False on failure."""
        try:
            await MCPClient().disconnect_all()
        except BaseException as e:
            logger.error("Error disconnecting MCPClient connections: %s", e)
            return False
        return True

    async def close(self) -> None:
        """Close all connections and release all resources."""
        try:
            await MCPClient().close()
        except BaseException as e:
            logger.error("Error closing MCPClient: %s", e)

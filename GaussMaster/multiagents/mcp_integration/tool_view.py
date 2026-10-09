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

"""Full tool view layer - built-in tools and MCP tools."""

import logging
from collections.abc import Iterable
from typing import Any

from GaussMaster.multiagents.mcp.mcp_client import MCPClient

from .mcp_tool_adapter import MCPToolAdapter

logger = logging.getLogger(__name__)


class ToolView:
    """Full tool view: built-in tools union MCP tools."""

    def __init__(self) -> None:
        self._adapter_cache: dict[str, tuple[object, MCPToolAdapter]] = {}
        self._warned_conflicts: set[str] = set()

    def _mcp_adapters(self, builtin_names: Iterable[str] | None = None) -> dict[str, MCPToolAdapter]:
        """Generate a name -> MCPToolAdapter mapping from the current MCPClient snapshot.

        Args:
            builtin_names: Built-in tool names already resolved by the caller
        """
        client = MCPClient()
        current = client.tools
        for name in [n for n, (tool, _) in self._adapter_cache.items()
                     if current.get(n) is not tool]:
            del self._adapter_cache[name]

        adapters: dict[str, MCPToolAdapter] = {}
        if builtin_names is None:
            builtin_names = self._builtin_tools().keys()

        for name, tool in current.items():
            cached = self._adapter_cache.get(name)
            if cached is None:
                if name in builtin_names:
                    if name not in self._warned_conflicts:
                        self._warned_conflicts.add(name)
                        logger.warning(
                            "MCP tool '%s' conflicts with a built-in tool; the view layer "
                            "resolves in favor of the built-in tool (this MCP tool is invisible)",
                            name)
                    continue
                adapter = MCPToolAdapter(tool, client, name)
                self._adapter_cache[name] = (tool, adapter)
                logger.info("MCP tool added by the view layer: %s", name)
            else:
                adapter = cached[1]
            adapters[name] = adapter
        return adapters

    def _builtin_tools(self) -> dict:
        import GaussMaster.common.utils
        from GaussMaster.multiagents.tools import base_tools
        return base_tools.all

    @property
    def all(self) -> dict:
        """Full tool dict (built-in takes precedence)."""
        builtins = self._builtin_tools()
        merged = self._mcp_adapters(builtin_names=builtins.keys())
        merged.update(builtins)
        return merged

    @property
    def detail_str_list(self) -> tuple[list[str], list[str]]:
        """Tool detail string lists: (with parameter lists, without parameter lists)."""
        with_param, without_param = [], []
        for tool in self.all.values():
            with_param.append(tool.__detail_with_param_str__)
            without_param.append(tool.__detail_without_param_str__)
        return with_param, without_param

    @property
    def detail_dict_list(self) -> tuple[list[dict], list[dict]]:
        """Tool detail dict lists: (with parameter lists, without parameter lists)."""
        with_param, without_param = [], []
        for tool in self.all.values():
            with_param.append(tool.__detail_with_param_dict__)
            without_param.append(tool.__detail_without_param_dict__)
        return with_param, without_param

    def get(self, name: str, default_value: Any = None) -> Any:
        """Get a tool by name; returns the default value if not found."""
        builtins = self._builtin_tools()
        builtin = builtins.get(name)
        if builtin is not None:
            return builtin
        return self._mcp_adapters(builtin_names=builtins.keys()).get(name, default_value)


all_tools = ToolView()

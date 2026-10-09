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

"""Component aggregation registry - cross-connection component aggregation and tool routing."""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from mcp_types import Prompt, Resource, Tool

if TYPE_CHECKING:
    from ._server_connection import ServerConnection


@dataclass
class _ComponentNames:
    """Reverse record of one registration."""

    prompt_names: set[str] = field(default_factory=set)
    resource_names: set[str] = field(default_factory=set)
    tool_names: set[str] = field(default_factory=set)


class ComponentRegistry:
    """Component aggregation registry: aggregation, tool routing, and conflict validation."""

    def __init__(self) -> None:
        self._prompts: dict[str, Prompt] = {}
        self._resources: dict[str, Resource] = {}
        self._tools: dict[str, Tool] = {}
        self._tool_to_conn: dict[str, "ServerConnection"] = {}

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Return a read-only view of the aggregated tool dict."""
        return MappingProxyType(self._tools)

    @staticmethod
    def _component_name(name: str, server_config_name: str) -> str:
        return f"{server_config_name}_{name}"

    def resolve_tool(self, tool_name: str) -> tuple["ServerConnection", str] | None:
        """Resolve routing info by tool name; returns None if not found."""
        tool = self._tools.get(tool_name)
        if tool is None:
            return None
        return self._tool_to_conn[tool_name], tool.name

    def register(
        self,
        conn: "ServerConnection",
        server_config_name: str,
        prompts: list[Prompt],
        resources: list[Resource],
        tools: list[Tool],
    ) -> _ComponentNames:
        """Register the components of one connection into the aggregated dicts.

        Args:
            conn:                Connection object owning this registration
            server_config_name:  Server name from the configuration (component name prefix)
            prompts:             Prompts list
            resources:           Resources list
            tools:               Tools list

        Returns:
            _ComponentNames: Reverse record of this registration

        Raises:
            ValueError: Duplicate or conflicting component names
        """
        prompts_temp: dict[str, Prompt] = {}
        for prompt in prompts:
            name = self._component_name(prompt.name, server_config_name)
            if name in prompts_temp:
                raise ValueError(f"Duplicate prompt name returned by the server: {name}")
            prompts_temp[name] = prompt

        resources_temp: dict[str, Resource] = {}
        for resource in resources:
            name = self._component_name(resource.name, server_config_name)
            if name in resources_temp:
                raise ValueError(f"Duplicate resource name returned by the server: {name}")
            resources_temp[name] = resource

        tools_temp: dict[str, Tool] = {}
        for tool in tools:
            name = self._component_name(tool.name, server_config_name)
            if name in tools_temp:
                raise ValueError(f"Duplicate tool name returned by the server: {name}")
            tools_temp[name] = tool

        matching_prompts = prompts_temp.keys() & self._prompts.keys()
        if matching_prompts:
            raise ValueError(
                f"{', '.join(sorted(matching_prompts))} already exist in prompts"
            )
        matching_resources = resources_temp.keys() & self._resources.keys()
        if matching_resources:
            raise ValueError(
                f"{', '.join(sorted(matching_resources))} already exist in resources"
            )
        matching_tools = tools_temp.keys() & self._tools.keys()
        if matching_tools:
            raise ValueError(
                f"{', '.join(sorted(matching_tools))} already exist in tools"
            )

        record = _ComponentNames(
            prompt_names=set(prompts_temp),
            resource_names=set(resources_temp),
            tool_names=set(tools_temp),
        )
        self._prompts.update(prompts_temp)
        self._resources.update(resources_temp)
        self._tools.update(tools_temp)
        self._tool_to_conn.update(
            {name: conn for name in record.tool_names}
        )
        return record

    def unregister(self, components: _ComponentNames) -> None:
        """Remove the aggregated components of one registration."""
        for name in components.prompt_names:
            self._prompts.pop(name, None)
        for name in components.resource_names:
            self._resources.pop(name, None)
        for name in components.tool_names:
            self._tools.pop(name, None)
            self._tool_to_conn.pop(name, None)

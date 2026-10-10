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

"""MCP tool adapter - adapts an MCP Tool into a callable compatible with the ToolView layer."""

import copy
import logging
from typing import Any

from mcp_types import Tool

from ..mcp.mcp_client import MCPClient

logger = logging.getLogger(__name__)


class MCPToolAdapter:
    """Adapt an MCP Tool into a callable consistent with built-in tools."""

    def __init__(self, tool: Tool, client: MCPClient, name: str) -> None:
        self._tool = tool
        self._client: MCPClient = client

        self.__func_name__ = name
        self.__description__ = tool.description or ""
        self.__param_dict_list__ = self._convert_input_schema()
        self.__detail_with_param_str__ = self._build_detail_str(with_param=True)
        self.__detail_without_param_str__ = self._build_detail_str(with_param=False)
        self.__detail_with_param_dict__ = self._build_detail_dict(with_param=True)
        self.__detail_without_param_dict__ = self._build_detail_dict(with_param=False)

    def _convert_input_schema(self) -> list[dict]:
        """Convert the MCP Tool inputSchema into the __param_dict_list__ format."""
        schema = getattr(self._tool, "input_schema", None) or getattr(self._tool, "inputSchema", None)
        if not schema or "properties" not in schema:
            return []
        required = set(schema.get("required", []))
        return [
            {
                "name": name,
                "type": prop.get("type", "string"),
                "description": prop.get("description", ""),
                "required": name in required,
            }
            for name, prop in schema["properties"].items()
        ]

    def _build_detail_str(self, with_param: bool = True) -> str:
        """Generate the tool description string."""
        desc = self.__func_name__ + "："
        if self.__description__:
            desc += self.__description__
            if with_param:
                desc += "，"
        if with_param:
            if self.__param_dict_list__:
                desc += "Parameter list: " + str(self.__param_dict_list__)
            else:
                desc += "Parameter list: none"
        return desc

    def _build_detail_dict(self, with_param: bool = True) -> dict[str, Any]:
        """Generate the tool description dict."""
        detail = {
            "name": self.__func_name__,
            "description": self.__description__,
        }
        if with_param:
            detail["arguments"] = self.__param_dict_list__
        return detail

    def has_correct_params(self, given_params: dict[str, Any]) -> tuple[bool, dict, dict]:
        """Validate MCP tool parameters."""
        target_params = copy.deepcopy(given_params)
        need_params = {
            p['name']: False
            for p in self.__param_dict_list__
            if p.get('required', True)
        }
        param_names = {p['name'] for p in self.__param_dict_list__}
        for given_key in list(given_params.keys()):
            if given_key not in param_names:
                target_params.pop(given_key)
            elif given_key in need_params:
                need_params.pop(given_key)
        if need_params:
            return False, {}, need_params
        return True, target_params, {}

    async def __call__(self, **kwargs: Any) -> str:
        """Call the MCP tool asynchronously."""
        try:
            return await self._client.call_tool(self.__func_name__, kwargs)
        except Exception as e:
            logger.error("Error calling MCP tool '%s'", self.__func_name__, exc_info=True)
            return f"Error calling MCP tool '{self.__func_name__}': {e}"

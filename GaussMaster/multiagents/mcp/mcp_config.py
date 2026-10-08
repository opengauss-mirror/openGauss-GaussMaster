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

"""MCP server config loading module."""

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from mcp.client.session_group import StreamableHttpParameters
from mcp.client.stdio import StdioServerParameters


class MCPServersConfigError(Exception):
    """Unified exception for MCP config loading or parsing failures."""


class TransportType(str, Enum):
    """MCP server transport type (mixed with str so logs and f-strings show the raw value instead of the enum name)."""

    STDIO = "stdio"
    HTTP = "http"

    __str__ = str.__str__


@dataclass
class StdioServerConfig:
    """Configuration of an MCP server using the stdio transport."""
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    @property
    def transport(self) -> TransportType:
        return TransportType.STDIO

    def to_server_params(self) -> StdioServerParameters:
        """Map the configuration to the mcp SDK stdio transport parameters."""
        return StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env or None,
        )


@dataclass
class HttpServerConfig:
    """Configuration of an MCP server using the HTTP transport."""
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0

    @property
    def transport(self) -> TransportType:
        return TransportType.HTTP

    def to_server_params(self) -> StreamableHttpParameters:
        """Map the configuration to the mcp SDK Streamable HTTP transport parameters."""
        return StreamableHttpParameters(
            url=self.url,
            headers=self.headers or None,
            timeout=self.timeout,
        )


@dataclass
class MCPServersConfig:
    """MCP server list configuration, loaded from a JSON config file."""
    servers: dict[str, StdioServerConfig | HttpServerConfig] = field(default_factory=dict)

    @classmethod
    def from_file(cls, config_path: str | Path) -> "MCPServersConfig":
        """Load the MCP server list from a JSON config file.

        Args:
            config_path: Config file path

        Returns:
            MCPServersConfig instance

        Raises:
            MCPServersConfigError: Config file missing, unreadable, or invalid
        """
        path = Path(config_path).expanduser()
        try:
            if not path.exists():
                raise MCPServersConfigError(f"MCP config file does not exist: {path}")
            raw = _load_json(path)
            return cls(servers=_parse_mcp_servers(raw))
        except MCPServersConfigError:
            raise
        except json.JSONDecodeError as exc:
            raise MCPServersConfigError(
                f"Config file is not valid JSON: {path}"
                f"(line {exc.lineno}, column {exc.colno}: {exc.msg})"
            ) from exc
        except UnicodeDecodeError as exc:
            raise MCPServersConfigError(
                f"Config file is not valid UTF-8: {path}: {exc}"
            ) from exc
        except OSError as exc:
            raise MCPServersConfigError(f"Failed to read the config file: {path}: {exc}") from exc
        except ValueError as exc:
            raise MCPServersConfigError(f"Invalid config file content: {exc}") from exc

    @property
    def is_empty(self) -> bool:
        return len(self.servers) == 0


def _load_json(path: Path) -> Any:
    """Read and parse the config file into raw JSON data."""
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f, object_pairs_hook=_reject_duplicate_keys)


def _parse_mcp_servers(raw: Any) -> dict[str, StdioServerConfig | HttpServerConfig]:
    """Validate the top-level structure and parse servers one by one."""
    if not isinstance(raw, dict):
        raise ValueError("The top level of the config file must be a JSON object")

    mcp_servers = raw.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        raise ValueError("The 'mcpServers' field in the config file must be an object")

    servers: dict[str, StdioServerConfig | HttpServerConfig] = {}
    for name, cfg in mcp_servers.items():
        if not name.strip():
            raise ValueError("Server name must be a non-empty string")
        if not isinstance(cfg, dict):
            raise ValueError(f"Configuration of server '{name}' must be an object")

        has_url = "url" in cfg
        has_command = "command" in cfg
        if has_url and has_command:
            raise ValueError(
                f"Ambiguous configuration for server '{name}': 'url' and 'command' are mutually exclusive"
            )

        if has_url:
            servers[name] = HttpServerConfig(
                url=_require_str(cfg, "url", name),
                headers=_require_str_dict(cfg, "headers", name),
                timeout=_require_number(cfg, "timeout", name, 30.0),
            )
        elif has_command:
            servers[name] = StdioServerConfig(
                command=_require_str(cfg, "command", name),
                args=_require_str_list(cfg, "args", name),
                env=_require_str_dict(cfg, "env", name),
            )
        else:
            raise ValueError(
                f"Invalid configuration for server '{name}': a 'command' (stdio) or 'url' (HTTP) field is required"
            )

    return servers


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Detect and reject duplicate JSON keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate key in the config file: '{key}'")
        result[key] = value
    return result


def _require_str(cfg: dict[str, Any], key: str, name: str) -> str:
    """Validate required non-empty string fields."""
    value = cfg[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' of server '{name}' must be a non-empty string")
    return value


def _require_number(cfg: dict[str, Any], key: str, name: str, default: float) -> float:
    """Validate optional numeric fields."""
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{key}' of server '{name}' must be a number")
    return float(value)


def _require_str_list(cfg: dict[str, Any], key: str, name: str) -> list[str]:
    """Validate optional string-array fields."""
    value = cfg.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"'{key}' of server '{name}' must be an array of strings")
    return value


def _require_str_dict(cfg: dict[str, Any], key: str, name: str) -> dict[str, str]:
    """Validate optional string-mapping fields."""
    value = cfg.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(item, str) for item in value.values()
    ):
        raise ValueError(f"'{key}' of server '{name}' must be a string-to-string mapping")
    return value

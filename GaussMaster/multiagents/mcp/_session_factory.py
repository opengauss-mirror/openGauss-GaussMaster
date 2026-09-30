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

"""Session factory - builds ClientSession by transport type."""

import logging
import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import httpx2
from mcp.client.session import ClientSession
from mcp.client.session_group import (
    ClientSessionParameters,
    SseServerParameters,
    StreamableHttpParameters,
)
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

logger = logging.getLogger(__name__)


@dataclass
class SessionContext:
    """A constructed MCP session.

    Attributes:
        session:                   The opened ClientSession
        transport_context_manager: Context manager of the underlying transport
        httpx_client:              httpx client; None for stdio transport
        errlog_file:               stdio stderr log file; None for HTTP transport
    """

    session: ClientSession
    transport_context_manager: AbstractAsyncContextManager[tuple[Any, Any]]
    httpx_client: httpx2.AsyncClient | None = None
    errlog_file: TextIO | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    async def close(self) -> None:
        """Close this session."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.session.__aexit__(None, None, None)
        except BaseException:
            logger.debug("session.__aexit__ cleanup failed", exc_info=True)
        await _close_transport_contexts(
            self.transport_context_manager, self.httpx_client
        )
        _close_errlog_file(self.errlog_file)


class SessionFactory:
    """Build ClientSession by transport type."""

    @classmethod
    async def create_session(
        cls,
        server_params: StdioServerParameters | SseServerParameters | StreamableHttpParameters,
        session_params: ClientSessionParameters | None = None,
        errlog_path: Path | None = None,
    ) -> SessionContext:
        """Build ClientSession by transport type and return an opened SessionContext.

        Args:
            server_params: StdioServerParameters / SseServerParameters / StreamableHttpParameters
            session_params: Session parameters; defaults are used when None
            errlog_path: Log file path for the stdio subprocess stderr

        Returns:
            SessionContext: The opened session binding

        Raises:
            NotImplementedError: SSE transport is not supported yet
            TypeError: Unsupported transport parameter type
        """
        if session_params is None:
            session_params = ClientSessionParameters()
        if isinstance(server_params, StdioServerParameters):
            return await cls._create_stdio_session(
                server_params, session_params, errlog_path
            )
        if isinstance(server_params, SseServerParameters):
            raise NotImplementedError("SSE transport is not supported yet")
        if isinstance(server_params, StreamableHttpParameters):
            return await cls._create_streamable_http_session(
                server_params, session_params
            )
        raise TypeError(
            f"Unsupported transport parameter type: {type(server_params).__name__}"
        )

    @classmethod
    async def _create_stdio_session(
        cls,
        server_params: StdioServerParameters,
        session_params: ClientSessionParameters,
        errlog_path: Path | None,
    ) -> SessionContext:
        """Build a stdio session."""
        errlog_file = _open_errlog_file(errlog_path) if errlog_path else None
        try:
            if errlog_file is not None:
                transport_context_manager = stdio_client(
                    server_params, errlog=errlog_file
                )
            else:
                transport_context_manager = stdio_client(server_params)
            try:
                read, write = await transport_context_manager.__aenter__()
            except BaseException:
                await _close_transport_contexts(transport_context_manager)
                raise
            return await cls._build_session(
                read,
                write,
                transport_context_manager,
                None,
                session_params,
                errlog_file=errlog_file,
            )
        except BaseException:
            _close_errlog_file(errlog_file)
            raise

    @classmethod
    async def _create_streamable_http_session(
        cls,
        server_params: StreamableHttpParameters,
        session_params: ClientSessionParameters,
    ) -> SessionContext:
        """Build a Streamable HTTP session."""
        httpx_client = create_mcp_http_client(
            headers=server_params.headers,
            timeout=httpx2.Timeout(
                server_params.timeout,
                read=server_params.sse_read_timeout,
            ),
        )
        try:
            await httpx_client.__aenter__()
        except BaseException:
            await httpx_client.aclose()
            raise

        transport_context_manager = streamable_http_client(
            url=server_params.url,
            http_client=httpx_client,
            terminate_on_close=server_params.terminate_on_close,
        )
        try:
            read, write = await transport_context_manager.__aenter__()
        except BaseException:
            await _close_transport_contexts(transport_context_manager, httpx_client)
            raise
        return await cls._build_session(
            read, write, transport_context_manager, httpx_client, session_params
        )

    @classmethod
    async def _build_session(
        cls,
        read: Any,
        write: Any,
        transport_context_manager: AbstractAsyncContextManager[tuple[Any, Any]],
        httpx_client: httpx2.AsyncClient | None,
        session_params: ClientSessionParameters,
        errlog_file: TextIO | None = None,
    ) -> SessionContext:
        """Construct ClientSession and open its context."""
        session = ClientSession(
            read,
            write,
            read_timeout_seconds=session_params.read_timeout_seconds,
            sampling_callback=session_params.sampling_callback,
            elicitation_callback=session_params.elicitation_callback,
            list_roots_callback=session_params.list_roots_callback,
            logging_callback=session_params.logging_callback,
            message_handler=session_params.message_handler,
            client_info=session_params.client_info,
        )
        try:
            await session.__aenter__()
        except BaseException:
            await _close_transport_contexts(transport_context_manager, httpx_client)
            raise
        return SessionContext(
            session=session,
            transport_context_manager=transport_context_manager,
            httpx_client=httpx_client,
            errlog_file=errlog_file,
        )


def stdio_errlog_path(server_config_name: str) -> Path:
    """Compute the stderr log file path for the stdio subprocess."""
    safe_name = re.sub(r"[^\w.-]", "_", server_config_name)
    return Path("logs") / "mcp_stdio" / f"{safe_name}.log"


def _open_errlog_file(path: Path) -> TextIO:
    """Open the stderr log file of the stdio subprocess."""
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.debug("stdio server stderr redirected to file: %s", path)
    return path.open("a", encoding="utf-8", errors="replace")


def _close_errlog_file(errlog_file: TextIO | None) -> None:
    """Close the errlog file."""
    if errlog_file is None:
        return
    try:
        errlog_file.close()
    except Exception:
        logger.debug("errlog file close failed", exc_info=True)


async def _close_transport_contexts(
    transport_context_manager: AbstractAsyncContextManager[tuple[Any, Any]],
    httpx_client: httpx2.AsyncClient | None = None,
) -> None:
    """Close transports and the httpx client."""
    try:
        await transport_context_manager.__aexit__(None, None, None)
    except BaseException:
        logger.debug("transport.__aexit__ compensating cleanup failed", exc_info=True)
    if httpx_client is not None:
        try:
            await httpx_client.aclose()
        except BaseException:
            logger.debug("httpx_client.aclose compensating cleanup failed", exc_info=True)

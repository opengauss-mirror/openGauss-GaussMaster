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

"""Health inspection supervisor."""

import asyncio
import logging
from collections.abc import Callable

from ._server_connection import ServerConnection
from ._server_status import HealthStatus

logger = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30.0
HEALTH_CHECK_PING_TIMEOUT = 5.0
UNREACHABLE_THRESHOLD = 3


class HealthSupervisor:
    """Health inspection supervisor: scheduling and triage dispatch only."""

    def __init__(self, connections: Callable[[], list[ServerConnection]]) -> None:
        self._get_connections = connections
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the inspection loop (idempotent)."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            self._task.add_done_callback(self._on_done)

    async def stop(self) -> None:
        """Stop the inspection loop (idempotent)."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None

    @staticmethod
    def _on_done(task: asyncio.Task) -> None:
        """Inspection loop done callback."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.critical("Health inspection coroutine exited unexpectedly: %s", exc, exc_info=exc)

    async def _run(self) -> None:
        """Main inspection loop."""
        while True:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                await self._round()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Health inspection error (skipping this round): %s", exc)
                continue

    async def _round(self) -> None:
        """One inspection round."""
        connections = [c for c in self._get_connections() if c.is_alive]
        results = await asyncio.gather(
            *(c.check_health(ping_timeout=HEALTH_CHECK_PING_TIMEOUT) for c in connections),
            return_exceptions=True,
        )
        for connection, outcome in zip(connections, results):
            if isinstance(outcome, BaseException):
                logger.debug(
                    "Unexpected error while inspecting server '%s'", connection.server_config_name, exc_info=outcome
                )
                outcome = HealthStatus.UNREACHABLE
            if outcome is HealthStatus.HEALTHY:
                connection.unreachable_count = 0
            elif outcome is HealthStatus.UNREACHABLE:
                connection.unreachable_count += 1
                logger.debug(
                    "Server '%s' transiently unreachable (%d/%d)",
                    connection.server_config_name,
                    connection.unreachable_count,
                    UNREACHABLE_THRESHOLD,
                )
                if connection.unreachable_count >= UNREACHABLE_THRESHOLD:
                    logger.warning(
                        "Server '%s' unreachable %d consecutive times; "
                        "disconnecting old connection for auto-reconnect",
                        connection.server_config_name,
                        UNREACHABLE_THRESHOLD,
                    )
                    try:
                        await connection.disconnect(keep_desired_connected=True)
                    except Exception:
                        logger.warning(
                            "Failed to disconnect old connection of server '%s'; "
                            "keeping the counter, retrying next inspection round",
                            connection.server_config_name,
                            exc_info=True,
                        )
                    else:
                        connection.unreachable_count = 0
            else:
                logger.warning(
                    "Detected disconnected connection of server '%s'; preparing auto-reconnect",
                    connection.server_config_name
                )
        for connection in self._get_connections():
            connection.reconnect()

"""A Mongo operation can't wait forever for an answer (second review,
finding 40).

pymongo waits forever for an answer on an open connection unless told
otherwise, and the app's client was given no bound. An answer lost on a
connection that stays open (a network fault, a stuck server) then held
the operation for good, and with it whatever lock its caller held: a
shielded admin action keeps the roles lock or an MCP's Stop/Start lock
until it ends.

    the URI bounds the wait (socketTimeoutMS or timeoutMS) -> the URI's bound
    otherwise                                              -> 30 s
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest
from pymongo.errors import PyMongoError

from mcpolis.adapters.repositories.mongo_client import (
    SOCKET_TIMEOUT_MS,
    MongoConnection,
)
from tests.unit.mongo_fixture import require_mongo


class AnswerLosingProxy:
    """A TCP proxy to a Mongo server that, once ``losing`` is set, stops
    passing the server's answers on: each connection stays open, and
    what is sent on it is never answered."""

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self.losing = False
        self._pumps: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._connect, "127.0.0.1", 0)
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for pump in self._pumps:
            pump.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)

    async def _connect(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter,
    ) -> None:
        server_reader, server_writer = await asyncio.open_connection(*self._target)
        for pump in (
            self._pump(client_reader, server_writer, answers=False),
            self._pump(server_reader, client_writer, answers=True),
        ):
            task = asyncio.create_task(pump)
            self._pumps.add(task)
            task.add_done_callback(self._pumps.discard)

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        answers: bool,
    ) -> None:
        try:
            while data := await reader.read(65536):
                if answers and self.losing:
                    continue
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()


def socket_timeout_of(connection: MongoConnection) -> float | None:
    timeout: float | None = connection.database.client.options.pool_options.socket_timeout
    return timeout


def test_a_connection_bounds_the_wait_for_an_answer() -> None:
    connection = MongoConnection("mongodb://127.0.0.1:27017", "mcpolis_test")

    assert socket_timeout_of(connection) == SOCKET_TIMEOUT_MS / 1000
    connection.close()


@pytest.mark.parametrize("uri, socket_timeout", [
    ("mongodb://127.0.0.1:27017/?socketTimeoutMS=5000", 5.0),
    ("mongodb://u:p%40ss@127.0.0.1:1,127.0.0.1:2/db?replicaSet=rs0&SOCKETTIMEOUTMS=7000", 7.0),
    ("mongodb://127.0.0.1:27017/?timeoutMS=10000", None),
])
def test_the_uris_own_bound_wins(uri: str, socket_timeout: float | None) -> None:
    connection = MongoConnection(uri, "mcpolis_test")

    assert socket_timeout_of(connection) == socket_timeout
    connection.close()


async def test_an_answer_lost_on_an_open_connection_fails_the_operation() -> None:
    target = urlsplit(require_mongo())
    proxy = AnswerLosingProxy(target.hostname or "127.0.0.1", target.port or 27017)
    port = await proxy.start()
    connection = MongoConnection(
        f"mongodb://127.0.0.1:{port}/?directConnection=true&retryReads=false"
        "&connectTimeoutMS=300&serverSelectionTimeoutMS=1000",
        "mcpolis_test_lost_answer",
        socket_timeout_ms=300,
    )
    probe = connection.database["probe"]
    try:
        assert await probe.find_one({}) is None  # answered through the proxy
        proxy.losing = True

        with pytest.raises(PyMongoError):
            await asyncio.wait_for(probe.find_one({}), timeout=5)
    finally:
        connection.close()
        await proxy.close()

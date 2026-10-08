"""Every background job the backend starts runs to its end.

The event loop keeps only weak references to tasks, so a task started
with ``asyncio.create_task`` and then dropped can be garbage-collected
before it finishes (see the ``asyncio.create_task`` docs). Each test
below starts a real background job through the production path, parks
it in a ``WeakGate`` (where only whoever started the job can keep it
alive), forces a collection, and checks the job still finishes.

The last tests guard the shape of the bug across the whole backend
source: a task dropped where it is started, handed to a caller that may
drop it, started through ``create_task`` passed on as a value (an alias,
a partial, a loop callback), or kept in a local the function never reads
again. A local that is read but not kept to the end is beyond a static
check; the behavior tests cover the risky sites.
"""
from __future__ import annotations

import ast
import asyncio
import tempfile
import textwrap
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.client.session import ClientSession
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.message import SessionMessage

from mcpolis.adapters.auth.mcp_gateway_oauth_provider import (
    McpGatewayOAuthProvider,
)
from mcpolis.adapters.auth.mcp_token_storage import McpTokenStorage
from mcpolis.adapters.auth.pending_auth import PendingAuthCoordinator
from mcpolis.adapters.gateway_session_registry import GatewaySessionRegistry
from mcpolis.adapters.repositories.connection_store import OAuthToken
from mcpolis.adapters.repositories.file_connection_store import (
    FileConnectionStore,
)
from mcpolis.adapters.upstream_clients.client_manager import (
    ConnectionTask,
    UpstreamClientManager,
)
from mcpolis.domain.model.policy import AuthMode
from mcpolis.domain.model.settings import (
    RoleDefinition,
    RoleSettings,
    SettingsConfig,
    UserDefinition,
)
from mcpolis.domain.model.upstream import TransportType, UpstreamDefinition
from mcpolis.domain.ports import DEFAULT_ORG_ID
from mcpolis.domain.ports.oauth_state_repository import (
    OAuthStateChanges,
    OAuthStateRepository,
    OAuthStateSnapshot,
    StoredAccessToken,
    StoredRefreshToken,
)
from mcpolis.domain.services.policy_engine import PolicyEngine
from mcpolis.domain.services.policy_notifier import PolicyNotifier
from mcpolis.domain.services.tool_registry import ToolRegistry
from mcpolis.domain.services.upstream_connection_service import (
    OAuthFailureReason,
    _start_background_token_acquisition,  # pyright: ignore[reportPrivateUsage]
    connect_and_refresh_tools,
    refresh_tools_in_background,
)
from tests.unit._weak_gate import WeakGate, make_weak_gate
from tests.unit.factories import (
    make_runtime_manager,
    make_upstream_auth,
    make_upstream_definition,
)

_SETTLE_SECONDS = 2.0
_SRC = Path(__file__).resolve().parents[2] / "src" / "mcpolis"
_ADMIN = "alice@example.com"


# ── A replaced hosted-MCP connection is closed in the background ──


class _ParkedCloseConnection:
    """Stand-in connection whose ``close`` parks in a ``WeakGate``."""

    def __init__(self, gate: WeakGate) -> None:
        self._gate = gate
        self.closed = asyncio.Event()

    async def close(self) -> None:
        await self._gate.pass_through()
        self.closed.set()


def make_parked_close_connection(gate: WeakGate) -> _ParkedCloseConnection:
    return _ParkedCloseConnection(gate)


async def test_a_replaced_shared_connection_still_closes() -> None:
    gate = make_weak_gate()
    replaced = make_parked_close_connection(gate)
    manager = UpstreamClientManager(upstreams=[])
    for connection in (replaced, make_parked_close_connection(make_weak_gate())):
        manager.transition_to_live_shared(
            "u1",
            session=cast(ClientSession, object()),
            task=cast(ConnectionTask, connection),
            server_info=None,
            self_description=None,
        )

    await gate.wait_until_parked()
    assert gate.open_after_collect(), (
        "the close of the replaced connection was garbage-collected"
    )
    await asyncio.wait_for(replaced.closed.wait(), _SETTLE_SECONDS)


# ── The dashboard's Start keeps running after its connect lands ──


async def test_a_start_job_is_still_held_after_its_connect_lands() -> None:
    gate = make_weak_gate()
    finished = asyncio.Event()
    manager = UpstreamClientManager(upstreams=[])

    async def start_job() -> None:
        # The connect lands: the upstream's record lets go of the job...
        manager.transition_to_live_shared(
            "u1",
            session=cast(ClientSession, object()),
            task=cast(
                ConnectionTask,
                make_parked_close_connection(make_weak_gate()),
            ),
            server_info=None,
            self_description=None,
        )
        # ...which still has work to do (refresh the tools, audit).
        await gate.pass_through()
        finished.set()

    manager.register_background_connect_task(
        "u1", asyncio.create_task(start_job()),
    )

    await gate.wait_until_parked()
    assert gate.open_after_collect(), (
        "the Start job was garbage-collected after its connect landed"
    )
    await asyncio.wait_for(finished.wait(), _SETTLE_SECONDS)


# ── Revoking a user's gateway tokens saves the sign-in store ──


class _ParkedSaveStateRepository(OAuthStateRepository):
    """Gateway sign-in store whose writes park in a ``WeakGate``."""

    def __init__(self, gate: WeakGate, snapshot: OAuthStateSnapshot) -> None:
        self._gate = gate
        self._snapshot = snapshot
        self.applied: list[OAuthStateChanges] = []
        self.save_done = asyncio.Event()

    async def load(self) -> OAuthStateSnapshot:
        return self._snapshot

    async def apply(self, changes: OAuthStateChanges) -> None:
        await self._gate.pass_through()
        self.applied.append(changes)
        self.save_done.set()


def make_snapshot_with_tokens(email: str) -> OAuthStateSnapshot:
    return OAuthStateSnapshot(
        access_tokens={
            "at": StoredAccessToken(
                token="at", client_id="c", user_email=email, scopes=[],
                expires_at=2**31,
            ),
        },
        refresh_tokens={
            "rt": StoredRefreshToken(
                token="rt", client_id="c", user_email=email, scopes=[],
                created_at=time.time(),
            ),
        },
    )


def make_gateway_oauth_provider(
    state_repository: OAuthStateRepository,
) -> McpGatewayOAuthProvider:
    return McpGatewayOAuthProvider(
        google_client_id="test-google-client-id",
        google_client_secret="test-google-secret",
        server_url="http://localhost:8000",
        runtime_manager=make_runtime_manager(PolicyEngine(SettingsConfig())),
        state_repository=state_repository,
    )


async def test_revoked_gateway_tokens_are_still_saved() -> None:
    gate = make_weak_gate()
    repo = _ParkedSaveStateRepository(
        gate, make_snapshot_with_tokens("alice@example.com"),
    )
    provider = make_gateway_oauth_provider(repo)
    await provider.load_state()

    assert provider.revoke_user_tokens("alice@example.com") == 2

    await gate.wait_until_parked()
    assert gate.open_after_collect(), (
        "the save of the revoked tokens was garbage-collected"
    )
    await asyncio.wait_for(repo.save_done.wait(), _SETTLE_SECONDS)
    assert repo.applied[-1].access_tokens == {"at": None}
    assert repo.applied[-1].refresh_tokens == {"rt": None}


# ── An MCP's "my list changed" notice refreshes it, then tells clients ──


class _FakeToolRegistry:
    """Tool catalog whose refreshes park in ``gate``, or answer at once
    without one. The "Fetching info" flag calls do nothing."""

    def __init__(self, gate: WeakGate | None) -> None:
        self._gate = gate

    def mark_refreshing(self, upstream_id: str) -> None:
        del upstream_id

    def unmark_refreshing(self, upstream_id: str) -> None:
        del upstream_id

    def refreshing_started_at(self, upstream_id: str) -> float | None:
        del upstream_id
        return None

    async def _refresh(self) -> None:
        if self._gate is not None:
            await self._gate.pass_through()

    async def refresh_upstream(
        self, upstream_id: str, session: ClientSession | None = None,
    ) -> list[object]:
        del upstream_id, session
        await self._refresh()
        return []

    async def refresh_resources_for_upstream(self, upstream_id: str) -> None:
        del upstream_id
        await self._refresh()

    async def refresh_prompts_for_upstream(self, upstream_id: str) -> None:
        del upstream_id
        await self._refresh()


def make_tool_registry(gate: WeakGate | None = None) -> ToolRegistry:
    return cast(ToolRegistry, _FakeToolRegistry(gate))


class _RecordingWriteStream:
    """The write end of a connected client's session."""

    def __init__(self) -> None:
        self.sent = asyncio.Event()

    def send_nowait(self, message: SessionMessage) -> None:
        del message
        self.sent.set()


class _FakeTransport:
    def __init__(self, write_stream: _RecordingWriteStream) -> None:
        self._write_stream = write_stream


class _FakeSessionManager:
    def __init__(self, sessions: dict[str, _FakeTransport]) -> None:
        self._server_instances = sessions


def make_list_change_notifier(
    gate: WeakGate, sessions: dict[str, _FakeTransport],
) -> tuple[PolicyNotifier, GatewaySessionRegistry]:
    config = SettingsConfig(
        users={"alice@example.com": UserDefinition(role="viewer")},
        roles={"viewer": RoleDefinition(settings=RoleSettings())},
    )
    registry = GatewaySessionRegistry()
    runtime_manager = make_runtime_manager(
        PolicyEngine(config), tool_registry=make_tool_registry(gate),
    )
    notifier = PolicyNotifier(
        cast(StreamableHTTPSessionManager, _FakeSessionManager(sessions)),
        registry,
        runtime_manager,
        debounce_seconds=0.0,
    )
    return notifier, registry


@pytest.mark.parametrize("changed", ["tools", "resources", "prompts"])
async def test_a_list_change_refresh_still_notifies_clients(
    changed: str,
) -> None:
    gate = make_weak_gate()
    stream = _RecordingWriteStream()
    notifier, registry = make_list_change_notifier(
        gate, {"s1": _FakeTransport(stream)},
    )
    registry.register("s1", DEFAULT_ORG_ID, "alice@example.com")
    notify: dict[str, Callable[[str, str], None]] = {
        "tools": notifier.notify_upstream_tools_changed,
        "resources": notifier.notify_upstream_resources_changed,
        "prompts": notifier.notify_upstream_prompts_changed,
    }

    notify[changed](DEFAULT_ORG_ID, "github")

    await gate.wait_until_parked()
    assert gate.open_after_collect(), (
        f"the {changed} refresh was garbage-collected"
    )
    await asyncio.wait_for(stream.sent.wait(), _SETTLE_SECONDS)


# ── The dashboard's "Refresh tools" runs in the background ──


class _ParkedConnectClientManager:
    """Hosted-MCP connections whose connect parks in a ``WeakGate``."""

    def __init__(self, gate: WeakGate) -> None:
        self._gate = gate

    async def ensure_shared_connected(
        self, upstream: UpstreamDefinition,
    ) -> ClientSession:
        del upstream
        await self._gate.pass_through()
        return cast(ClientSession, object())


async def test_a_tool_refresh_still_finishes_after_its_caller_drops_it() -> None:
    gate = make_weak_gate()
    finished = asyncio.Event()

    async def on_success() -> None:
        finished.set()

    # Like the "Refresh tools" endpoint: start it, drop the returned task.
    refresh_tools_in_background(
        org_id="o1",
        upstream=make_upstream_definition(id="u1", command="npx"),
        effective_user="",
        connection_store=None,
        client_manager=cast(
            UpstreamClientManager, _ParkedConnectClientManager(gate),
        ),
        tool_registry=make_tool_registry(),
        server_url="http://localhost:8000",
        on_success=on_success,
    )

    await gate.wait_until_parked()
    assert gate.open_after_collect(), (
        "the tool refresh was garbage-collected"
    )
    await asyncio.wait_for(finished.wait(), _SETTLE_SECONDS)


# ── A sign-in waits in the background for its browser step ──


class _ParkedThenAbandonedSignIn(httpx.Auth):
    """A sign-in whose browser step parks in a ``WeakGate``, then is
    abandoned (the user closed the tab). It raises before sending any
    request, so no network is touched."""

    def __init__(self, gate: WeakGate) -> None:
        self._gate = gate

    async def async_auth_flow(
        self, request: httpx.Request,
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        await self._gate.pass_through()
        raise RuntimeError("the user closed the sign-in tab")
        yield request  # makes this an async generator, as httpx expects


def make_oauth_upstream() -> UpstreamDefinition:
    return make_upstream_definition(
        id="mixpanel",
        transport=TransportType.streamable_http,
        url="http://127.0.0.1:9/mcp",
        auth=make_upstream_auth(mode=AuthMode.admin_oauth),
    )


async def test_a_sign_in_still_reports_its_outcome_after_its_caller_drops_it() -> None:
    gate = make_weak_gate()
    reported: list[OAuthFailureReason] = []
    done = asyncio.Event()

    def on_error(message: str, reason: OAuthFailureReason) -> None:
        del message
        reported.append(reason)
        done.set()

    upstream = make_oauth_upstream()
    pending = PendingAuthCoordinator(b"k" * 32).create_pending(
        DEFAULT_ORG_ID, upstream.id, _ADMIN,
    )
    with tempfile.TemporaryDirectory() as root:
        store = FileConnectionStore(Path(root))
        # Like initiate_oauth_connection: start it, drop the returned task.
        _start_background_token_acquisition(
            upstream,
            cast(OAuthClientProvider, _ParkedThenAbandonedSignIn(gate)),
            McpTokenStorage(store, DEFAULT_ORG_ID, upstream.id, _ADMIN),
            pending,
            on_error=on_error,
        )

        await gate.wait_until_parked()
        assert gate.open_after_collect(), "the sign-in was garbage-collected"
        await asyncio.wait_for(done.wait(), _SETTLE_SECONDS)

    assert reported == [OAuthFailureReason.token_exchange]
    assert pending.failure_message is not None


# ── An OAuth connect refreshes the tool catalog after it answers ──


class _InstantSignInClientManager:
    """Connects a user's session at once, from the stored tokens."""

    async def replace_user_session(
        self, upstream: UpstreamDefinition, user: str, *,
        auth: httpx.Auth | None = None,
    ) -> None:
        del upstream, user, auth

    def is_stopped(self, upstream_id: str) -> bool:
        """Never stopped by an admin."""
        del upstream_id
        return False


def make_valid_token() -> OAuthToken:
    return OAuthToken(
        access_token="access-stub",
        refresh_token="refresh-stub",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["read"],
    )


async def test_an_oauth_connect_still_refreshes_the_catalog_after_answering() -> None:
    gate = make_weak_gate()
    refreshed = asyncio.Event()
    upstream = make_oauth_upstream()
    with tempfile.TemporaryDirectory() as root:
        store = FileConnectionStore(Path(root))
        await store.put_user_token(
            DEFAULT_ORG_ID, _ADMIN, upstream.id, make_valid_token(),
        )

        result = await connect_and_refresh_tools(
            org_id=DEFAULT_ORG_ID,
            upstream=upstream,
            effective_user=_ADMIN,
            connection_store=store,
            auth_coordinator=PendingAuthCoordinator(b"k" * 32),
            client_manager=cast(
                UpstreamClientManager, _InstantSignInClientManager(),
            ),
            tool_registry=make_tool_registry(gate),
            server_url="http://localhost:8000",
            on_tools_refreshed=refreshed.set,
        )

        assert result.connected
        await gate.wait_until_parked()
        assert gate.open_after_collect(), (
            "the catalog refresh was garbage-collected"
        )
        await asyncio.wait_for(refreshed.wait(), _SETTLE_SECONDS)


# ── No line in the backend drops a task it starts ──

# Calls that start a task, or a future that owns one; dropping their
# result loses it.
_TASK_STARTERS = frozenset({
    "create_task", "ensure_future", "Task",
    "gather", "shield", "run_coroutine_threadsafe",
})
# The ones a ``return`` or a lambda must not hand on. Returning a gather
# or a shield to a caller that awaits it is the normal idiom.
_HANDED_ON_STARTERS = frozenset({"create_task", "ensure_future", "Task"})


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _dropped_starts(
    expr: ast.expr, starters: frozenset[str],
) -> list[ast.Call]:
    """The task starts whose result ``expr`` throws away.

    Follows only the places a value is lost: a method called on the new
    task (``create_task(c).add_done_callback(cb)``), both branches of a
    conditional, the items of a literal or a boolean chain, the element
    of a comprehension. A start passed as an argument is the callee's to
    hold, and one under ``await`` is held until it ends.
    """
    if isinstance(expr, ast.Call):
        if _called_name(expr) in starters:
            return [expr]
        if isinstance(expr.func, ast.Attribute):
            return _dropped_starts(expr.func.value, _TASK_STARTERS)
        return []
    if isinstance(expr, ast.IfExp):
        return (
            _dropped_starts(expr.body, starters)
            + _dropped_starts(expr.orelse, starters)
        )
    if isinstance(expr, ast.BoolOp):
        items = expr.values
    elif isinstance(expr, ast.Tuple | ast.List | ast.Set):
        items = expr.elts
    elif isinstance(expr, ast.ListComp | ast.SetComp | ast.GeneratorExp):
        items = [expr.elt]
    else:
        return []
    return [call for item in items for call in _dropped_starts(item, starters)]


def _held_by_task_group(
    call: ast.Call, parents: dict[ast.AST, ast.AST],
) -> bool:
    """``tg.create_task(...)`` inside ``async with ... as tg:`` or
    ``async with tg:``: the group holds its tasks until the block ends."""
    if not isinstance(call.func, ast.Attribute):
        return False
    receiver = ast.unparse(call.func.value)
    node = parents.get(call)
    while node is not None:
        if isinstance(node, ast.AsyncWith):
            for item in node.items:
                bound = [ast.unparse(item.context_expr)]
                if item.optional_vars is not None:
                    bound.append(ast.unparse(item.optional_vars))
                if receiver in bound:
                    return True
        node = parents.get(node)
    return False


def _starters_used_as_values(
    tree: ast.AST, parents: dict[ast.AST, ast.AST],
) -> list[ast.expr]:
    """``create_task`` / ``ensure_future`` referred to without being
    called on the spot: an alias (``start = asyncio.create_task``), a
    ``functools.partial``, a loop callback
    (``loop.call_soon(asyncio.create_task, coro)``). Whoever calls them
    later drops the task, and no check sees that call."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        else:
            continue
        if name not in _HANDED_ON_STARTERS - {"Task"}:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and parent.func is node:
            continue
        found.append(node)
    return found


def _forgotten_local_tasks(tree: ast.AST) -> list[ast.stmt]:
    """``task = asyncio.create_task(...)`` in a function that never reads
    ``task`` again (``del task`` included): kept in a local, then dropped
    when the function returns."""
    found: list[ast.stmt] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        read = {
            node.id for node in ast.walk(function)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if (
                isinstance(target, ast.Name)
                and target.id not in read
                and isinstance(value, ast.Call)
                and _called_name(value) in _HANDED_ON_STARTERS
            ):
                found.append(node)
    return found


def _unheld_task_starts(source: str) -> list[int]:
    """Lines where ``source`` drops a task it starts: as a bare statement,
    handed to a caller that may drop it (``return``, a lambda, the task
    starter itself passed on as a value), or kept in a local the function
    never reads again. A local that is read but not kept to the end is
    beyond a static check; the behavior tests cover the risky sites."""
    tree = ast.parse(source)
    parents = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }
    dropped: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr):
            dropped += _dropped_starts(node.value, _TASK_STARTERS)
        elif isinstance(node, ast.Return) and node.value is not None:
            dropped += _dropped_starts(node.value, _HANDED_ON_STARTERS)
        elif isinstance(node, ast.Lambda):
            dropped += _dropped_starts(node.body, _HANDED_ON_STARTERS)
    lines = {
        call.lineno for call in dropped
        if not _held_by_task_group(call, parents)
    }
    lines |= {node.lineno for node in _starters_used_as_values(tree, parents)}
    lines |= {node.lineno for node in _forgotten_local_tasks(tree)}
    return sorted(lines)


def test_no_line_in_the_backend_drops_a_task_it_starts() -> None:
    offenders = [
        f"{path.relative_to(_SRC)}:{line}"
        for path in sorted(_SRC.rglob("*.py"))
        for line in _unheld_task_starts(path.read_text())
    ]
    assert offenders == [], (
        "These lines start an asyncio task and drop it, or hand it to a "
        "caller that may drop it, so it can be garbage-collected before "
        "it finishes. Start it with BackgroundTaskSet.spawn "
        "(mcpolis/domain/services/background_tasks.py), or keep the "
        "task yourself:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(("source", "flagged"), [
    pytest.param("asyncio.create_task(job())", True, id="bare create_task"),
    pytest.param("loop.create_task(job())", True, id="bare loop.create_task"),
    pytest.param("asyncio.ensure_future(job())", True, id="bare ensure_future"),
    pytest.param(
        "asyncio.create_task(job()).add_done_callback(cb)", True,
        id="method called on the dropped task",
    ),
    pytest.param(
        "loop.call_soon(lambda: asyncio.create_task(job()))", True,
        id="lambda hands it to call_soon",
    ),
    pytest.param(
        "[asyncio.create_task(j) for j in jobs]", True,
        id="comprehension statement",
    ),
    pytest.param(
        "asyncio.create_task(job()) if ok else None", True,
        id="conditional statement",
    ),
    pytest.param("asyncio.gather(a(), b())", True, id="gather not awaited"),
    pytest.param(
        "asyncio.run_coroutine_threadsafe(job(), loop)", True,
        id="run_coroutine_threadsafe dropped",
    ),
    pytest.param(
        "def f():\n    return asyncio.create_task(job())", True,
        id="returned to a caller",
    ),
    pytest.param(
        """
        async def f():
            async with asyncio.TaskGroup() as tg:
                tg.create_task(job())
        def g(tg):
            tg.create_task(job())
        """, True, id="task group name reused outside its block",
    ),
    pytest.param(
        """
        def f():
            start = asyncio.create_task
            start(job())
        """, True, id="starter aliased",
    ),
    pytest.param(
        "functools.partial(asyncio.create_task, job())()", True,
        id="starter in a partial",
    ),
    pytest.param(
        "loop.call_soon(asyncio.create_task, job())", True,
        id="starter as a loop callback",
    ),
    pytest.param(
        "loop.call_soon(asyncio.ensure_future, job())", True,
        id="ensure_future as a loop callback",
    ),
    pytest.param(
        """
        async def f():
            task = asyncio.create_task(job())
            del task
        """, True, id="local then forgotten",
    ),
    pytest.param(
        """
        async def f():
            task: asyncio.Task[None] = asyncio.create_task(job())
        """, True, id="annotated local never read",
    ),
    pytest.param(
        "async def f():\n    await asyncio.create_task(job())", False,
        id="awaited",
    ),
    pytest.param(
        "task = asyncio.create_task(job())", False, id="kept in a variable",
    ),
    pytest.param(
        """
        async def f():
            task = asyncio.create_task(job())
            tasks.hold(task)
        """, False, id="local handed to a holder",
    ),
    pytest.param(
        """
        def f(self):
            self._task = asyncio.create_task(job())
        """, False, id="kept on an object",
    ),
    pytest.param("tasks.spawn(job())", False, id="spawned through a holder"),
    pytest.param(
        "tasks.hold(asyncio.create_task(job()))", False,
        id="passed to a holder",
    ),
    pytest.param(
        "def f():\n    return asyncio.gather(a(), b())", False,
        id="gather returned to an awaiting caller",
    ),
    pytest.param(
        """
        async def f():
            async with asyncio.TaskGroup() as tg:
                tg.create_task(job())
        """, False, id="task group",
    ),
    pytest.param(
        """
        async def f():
            tg = asyncio.TaskGroup()
            async with tg:
                tg.create_task(job())
        """, False, id="task group bound by assignment",
    ),
    pytest.param(
        """
        class C:
            async def f(self):
                async with self._tg:
                    self._tg.create_task(job())
        """, False, id="task group held on self",
    ),
])
def test_the_shape_check_flags_dropped_tasks_only(
    source: str, flagged: bool,
) -> None:
    assert bool(_unheld_task_starts(textwrap.dedent(source))) == flagged

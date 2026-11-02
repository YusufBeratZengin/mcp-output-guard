"""The stdio proxy: two pumps, one table of pending requests.

    client stdout <-- [proxy] <-- server stdout
    client stdin  --> [proxy] --> server stdin

Everything that is not a ``tools/call`` result (or ``tools/list``) is forwarded
as the original bytes, which is what makes the "under the limit, byte for byte"
guarantee cheap and provable.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, Any, BinaryIO

from . import framing, virtual
from .framing import FramingError
from .limits import DEFAULT_IMAGE_MAX_CHARS, ToolLimits, limit_result
from .spill import SpillStore

__all__ = ["GuardStats", "PendingCall", "Proxy", "resolve_command"]

_JSONRPC_VERSION = "2.0"


@dataclass
class PendingCall:
    method: str
    tool: str | None
    params: dict[str, Any]


@dataclass
class GuardStats:
    """Counters shown with ``--stats`` at shutdown."""

    calls_seen: int = 0
    results_truncated: int = 0
    chars_hidden: int = 0
    binaries_spilled: int = 0

    def summary(self) -> str:
        return (
            f"mcp-output-guard: {self.calls_seen} tool call(s), "
            f"{self.results_truncated} truncated, {self.chars_hidden} char(s) kept out "
            f"of the context, {self.binaries_spilled} binary block(s) spilled"
        )


def _id_key(value: Any) -> str:
    """Stable key for a JSON-RPC id (``1`` and ``"1"`` must not collide)."""
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - ids are scalars in practice
        return repr(value)


def resolve_command(argv: Sequence[str], *, platform_name: str | None = None) -> list[str]:
    """Return the argv to spawn, working around Windows ``.cmd`` shims.

    On Windows ``npx`` is ``npx.cmd``, which ``CreateProcess`` cannot run
    directly. Wrapping it in ``cmd /c`` is the standard fix and keeps the
    documented ``-- npx -y server`` invocation working.
    """
    args = list(argv)
    if not args:
        return args
    platform_name = platform_name or os.name
    if platform_name != "nt":
        return args
    found = shutil.which(args[0])
    target = found or args[0]
    if target.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", target, *args[1:]]
    return [target, *args[1:]]


class Proxy:
    """A transparent MCP stdio proxy with output guarding."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        max_chars: int,
        store: SpillStore,
        tool_limits: ToolLimits | None = None,
        image_max_chars: int = DEFAULT_IMAGE_MAX_CHARS,
        note_lang: str = "en",
        reserve_note: bool = False,
        virtual_tools: bool = True,
        client_in: BinaryIO | None = None,
        client_out: BinaryIO | None = None,
        log: BinaryIO | None = None,
        platform_name: str | None = None,
    ) -> None:
        if not command:
            raise ValueError("no MCP server command given")
        self.command = list(command)
        self.max_chars = max_chars
        self.store = store
        self.tool_limits = tool_limits or ToolLimits(max_chars)
        self.image_max_chars = image_max_chars
        self.note_lang = note_lang
        self.reserve_note = reserve_note
        self.virtual_tools = virtual_tools
        self.stats = GuardStats()

        self._client_in = client_in if client_in is not None else sys.stdin.buffer
        self._client_out = client_out if client_out is not None else sys.stdout.buffer
        self._log = log if log is not None else sys.stderr.buffer
        self._platform_name = platform_name

        self._pending: dict[str, PendingCall] = {}
        self._pending_lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._server_in: IO[bytes] | None = None
        self._server_out: IO[bytes] | None = None
        self._process: subprocess.Popen[bytes] | None = None

    # -- plumbing ------------------------------------------------------
    def _log_line(self, message: str) -> None:
        try:
            self._log.write((message + "\n").encode("utf-8"))
            self._log.flush()
        except (OSError, ValueError):  # pragma: no cover - stderr closed
            pass

    def _write_client(self, raw: bytes) -> None:
        with self._client_lock:
            framing.write_line(self._client_out, raw)

    def _write_server(self, raw: bytes) -> None:
        assert self._server_in is not None
        framing.write_line(self._server_in, raw)

    def _respond(self, request_id: Any, result: dict[str, Any]) -> None:
        message = {"jsonrpc": _JSONRPC_VERSION, "id": request_id, "result": result}
        self._write_client(framing.encode_message(message))

    # -- client side ---------------------------------------------------
    def handle_client_message(self, raw: bytes) -> bytes | None:
        """Return bytes to forward upstream, or ``None`` if answered locally."""
        try:
            message = framing.decode_message(raw)
        except FramingError:
            # Not ours to judge: let the server report the protocol error.
            return raw

        if isinstance(message, list):
            return self._handle_client_batch(message, raw)

        if not isinstance(message, dict):
            return raw
        method = message.get("method")
        params = message.get("params")
        params_dict: dict[str, Any] = params if isinstance(params, dict) else {}

        if method == "tools/call":
            name = params_dict.get("name")
            if self.virtual_tools and virtual.is_virtual_tool(name) and "id" in message:
                self.stats.calls_seen += 1
                result = virtual.call_virtual_tool(
                    str(name), params_dict.get("arguments"), self.store
                )
                self._respond(message["id"], result)
                return None
            self.stats.calls_seen += 1
            self._track(message.get("id"), PendingCall("tools/call", _as_str(name), params_dict))
            return raw

        if method == "tools/list":
            self._track(message.get("id"), PendingCall("tools/list", None, params_dict))
            return raw

        return raw

    def _handle_client_batch(self, message: list[Any], raw: bytes) -> bytes:
        # Batches are rare in MCP; pass them through, tracking call ids so the
        # response array still gets guarded.
        for item in message:
            if isinstance(item, dict) and item.get("method") in {"tools/call", "tools/list"}:
                params = item.get("params")
                params_dict = params if isinstance(params, dict) else {}
                self._track(
                    item.get("id"),
                    PendingCall(str(item.get("method")), _as_str(params_dict.get("name")), params_dict),
                )
        return raw

    def _track(self, request_id: Any, pending: PendingCall) -> None:
        if request_id is None:
            return
        with self._pending_lock:
            self._pending[_id_key(request_id)] = pending

    def _pop_pending(self, request_id: Any) -> PendingCall | None:
        with self._pending_lock:
            return self._pending.pop(_id_key(request_id), None)

    # -- server side ---------------------------------------------------
    def handle_server_message(self, raw: bytes) -> bytes:
        """Return the bytes to deliver downstream (the original ones if unmodified)."""
        try:
            message = framing.decode_message(raw)
        except FramingError:
            return raw

        if isinstance(message, list):
            handled = [self._handle_server_object(item) for item in message]
            if all(item is None for item in handled):
                return raw
            merged = [item if item is not None else message[i] for i, item in enumerate(handled)]
            return framing.encode_message(merged)

        if not isinstance(message, dict):
            return raw
        updated = self._handle_server_object(message)
        if updated is None:
            return raw
        return framing.encode_message(updated)

    def _handle_server_object(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or "id" not in message:
            return None
        pending = self._pop_pending(message["id"])
        if pending is None or not isinstance(message.get("result"), dict):
            return None

        if pending.method == "tools/list":
            first_page = not pending.params.get("cursor")
            if self.virtual_tools and self._inject_tools(message["result"], first_page=first_page):
                return message
            return None

        if pending.method != "tools/call":
            return None

        result = message["result"]
        if result.get("isError") is True:
            return None

        limit = self.tool_limits.for_tool(pending.tool)
        guarded, info = limit_result(
            result,
            limit,
            spill=self.store,
            tool=pending.tool,
            note_lang=self.note_lang,
            image_max_chars=self.image_max_chars,
            reserve_note=self.reserve_note,
        )
        if info is None:
            return None

        self.stats.results_truncated += 1
        self.stats.chars_hidden += max(0, int(info.get("total_chars") or 0) - int(info.get("shown_chars") or 0))
        self.stats.binaries_spilled += 1 if info.get("binary") else 0
        message["result"] = guarded
        return message

    @staticmethod
    def _inject_tools(result: dict[str, Any], *, first_page: bool = True) -> bool:
        tools = result.get("tools")
        if not isinstance(tools, list):
            return False
        # Paginated lists are concatenated by the client, so the synthetic tools
        # go on the first page only (the request that carries no cursor).
        if not first_page:
            return False
        present = {t.get("name") for t in tools if isinstance(t, dict)}
        added = [d for d in virtual.tool_definitions() if d["name"] not in present]
        if not added:
            return False
        tools.extend(added)
        return True

    # -- main loop -----------------------------------------------------
    def run(self) -> int:
        self.store.ensure()
        command = resolve_command(self.command, platform_name=self._platform_name)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=0,
            )
        except OSError as exc:
            self._log_line(f"mcp-output-guard: cannot start server {command[0]!r}: {exc}")
            return 127

        self._process = process
        assert process.stdin is not None and process.stdout is not None
        self._server_in = process.stdin
        self._server_out = process.stdout

        reader = threading.Thread(target=self._server_loop, name="guard-server", daemon=True)
        reader.start()

        try:
            self._client_loop()
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            process.terminate()
        finally:
            try:
                if self._server_in is not None:
                    self._server_in.close()
            except (BrokenPipeError, OSError):
                pass
            reader.join(timeout=10)
            try:
                code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - hung server
                process.kill()
                code = process.wait()
            with contextlib.suppress(OSError, ValueError):
                if process.stdout is not None:
                    process.stdout.close()
        return int(code)

    def _client_loop(self) -> None:
        while True:
            try:
                raw = framing.read_line(self._client_in)
            except (OSError, ValueError):
                break
            if raw is None:
                break
            if not raw.strip():
                continue
            out = self.handle_client_message(raw)
            if out is not None:
                try:
                    self._write_server(out)
                except (BrokenPipeError, OSError):
                    break

    def _server_loop(self) -> None:
        assert self._server_out is not None
        while True:
            try:
                raw = framing.read_line(self._server_out)
            except (OSError, ValueError):
                break
            if raw is None:
                break
            if not raw.strip():
                continue
            try:
                self._write_client(self.handle_server_message(raw))
            except (BrokenPipeError, OSError):
                break
        with self._client_lock, contextlib.suppress(OSError, ValueError):  # pragma: no cover
            self._client_out.flush()


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None

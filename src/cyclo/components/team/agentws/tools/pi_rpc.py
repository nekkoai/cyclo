# SPDX-License-Identifier: MIT
"""Client for the interactive agent's local Pi JSON-lines RPC subprocess."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable


class ProtocolError(RuntimeError):
    """Pi violated its RPC protocol."""


class CommandRejected(ProtocolError):
    """Pi rejected a command sent by this client."""


class ProcessExited(ProtocolError):
    """Pi exited before the current prompt settled."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Pi RPC process exited with status {status}")
        self.status = status


def command(
    model: str | None,
    session_dir: Path,
    *,
    append_system_prompt: str | None = None,
) -> list[str]:
    result = ["pi", "--mode", "rpc"]
    if model:
        result.extend(["--model", model])
    if append_system_prompt is not None:
        result.extend(["--append-system-prompt", append_system_prompt])
    result.extend(["--session-dir", str(session_dir)])
    return result


class Session:
    """Own JSONL framing and prompt settlement for one running Pi process."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        on_error: Callable[[ProtocolError], None] | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None:
            raise ValueError("Pi RPC requires piped stdin and stdout")
        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.on_event = on_event
        self.on_error = on_error
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._prompt_lock = threading.Lock()
        self._active_request: str | None = None
        self._active_result: queue.Queue[None | ProtocolError] | None = None
        self._error: ProtocolError | None = None
        self._eof = False
        self._exit_status = 1
        self.reader_thread = threading.Thread(
            target=self._read_events,
            name="pi-rpc-reader",
            daemon=True,
        )

    def start(self) -> None:
        self.reader_thread.start()

    def send_message(
        self,
        request_id: str,
        command_type: str,
        message: str,
    ) -> bool:
        return self._send(
            {"id": request_id, "type": command_type, "message": message}
        )

    def run_prompt(self, request_id: str, message: str) -> None:
        """Send one prompt and wait until Pi is fully settled."""

        with self._prompt_lock:
            result_queue: queue.Queue[None | ProtocolError] = queue.Queue(
                maxsize=1
            )
            with self._state_lock:
                if self._error is not None:
                    raise self._error
                if self._eof:
                    raise ProcessExited(self._exit_status)
                self._active_request = request_id
                self._active_result = result_queue

            try:
                if not self.send_message(request_id, "prompt", message):
                    raise ProtocolError("could not write a Pi RPC prompt")
                result = result_queue.get()
                if isinstance(result, ProtocolError):
                    raise result
                return None
            finally:
                with self._state_lock:
                    if self._active_result is result_queue:
                        self._active_request = None
                        self._active_result = None

    def _send(self, value: dict[str, Any]) -> bool:
        with self._write_lock:
            if self.stdin.closed:
                return False
            try:
                self.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
                self.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def _read_events(self) -> None:
        try:
            for raw in self.stdout:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProtocolError("Pi RPC emitted invalid JSON") from exc
                if not isinstance(event, dict):
                    raise ProtocolError("Pi RPC emitted a non-object JSON value")

                with self._state_lock:
                    rejected = bool(
                        event.get("type") == "response"
                        and event.get("id") == self._active_request
                        and event.get("success") is False
                    )
                    settled = event.get("type") == "agent_settled"

                # Rendering is part of consuming an event. Do not publish its
                # outcome until the callback has accepted it.
                if self.on_event is not None:
                    self.on_event(raw, event)

                if rejected:
                    self._complete_active(
                        CommandRejected(
                            str(event.get("error") or "Pi rejected the prompt")
                        )
                    )
                elif settled:
                    self._complete_active(None)
        except ProtocolError as exc:
            self._fail(exc)
        except Exception as exc:
            self._fail(ProtocolError(f"Pi RPC event handler failed: {exc}"))
        finally:
            status = self._process_status()
            with self._state_lock:
                self._eof = True
                self._exit_status = status
            self._complete_active(ProcessExited(status))

    def _complete_active(self, result: None | ProtocolError) -> None:
        with self._state_lock:
            result_queue = self._active_result
        if result_queue is None:
            return
        try:
            result_queue.put_nowait(result)
        except queue.Full:
            pass

    def _fail(self, error: ProtocolError) -> None:
        with self._state_lock:
            self._error = error
        self._complete_active(error)
        if self.on_error is not None:
            self.on_error(error)

    def _process_status(self) -> int:
        status = self.process.poll()
        if status is not None:
            return status
        try:
            return self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return 1

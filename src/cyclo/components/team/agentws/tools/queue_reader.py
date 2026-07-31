#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded, link-safe reads from an AgentWS queue."""

from __future__ import annotations

import errno
import os
import re
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


VALID_QUEUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VALID_TASK_STATES = frozenset({"open", "done"})
DEFAULT_MAX_ENTRIES = 4096
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_READ_BYTES = 16 * 1024 * 1024
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class QueueLimitError(OSError):
    """A queue read exceeded a fixed resource limit."""


def validate_queue_id(value: str, label: str = "queue ID") -> str:
    if not VALID_QUEUE_ID.fullmatch(value):
        raise ValueError(
            f"invalid {label} '{value}' - must start with an alphanumeric "
            "character and contain only alphanumeric, dot, underscore, and hyphen"
        )
    return value


def validate_task_state(value: str) -> str:
    if value not in VALID_TASK_STATES:
        expected = " ".join(sorted(VALID_TASK_STATES))
        raise ValueError(f"invalid task state '{value}' (expected: {expected})")
    return value


def terminal_safe(value: str) -> str:
    """Make terminal controls visible while retaining line and column layout."""

    escaped: list[str] = []
    for character in value:
        if character in {"\n", "\t"}:
            escaped.append(character)
            continue
        category = unicodedata.category(character)
        if category not in {"Cc", "Cf", "Cs"}:
            escaped.append(character)
            continue
        codepoint = ord(character)
        if codepoint <= 0xFF:
            escaped.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(f"\\U{codepoint:08x}")
    return "".join(escaped)


class ConfinedQueueReader:
    """Read an AgentWS queue without following links below or to its root."""

    def __init__(
        self,
        root: Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
    ):
        if not hasattr(os, "O_NOFOLLOW"):
            raise OSError(
                errno.ENOTSUP, "safe AgentWS queue reads require O_NOFOLLOW"
            )
        if max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        if max_file_bytes < 0:
            raise ValueError("max_file_bytes must be non-negative")
        if max_read_bytes < 0:
            raise ValueError("max_read_bytes must be non-negative")
        self.root = Path(os.path.abspath(Path(root).expanduser()))
        self.max_entries = max_entries
        self.max_file_bytes = max_file_bytes
        self.max_read_bytes = max_read_bytes
        self._entries_seen = 0
        self._bytes_read = 0
        self.root_fd = self._open_absolute_directory(self.root)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()

    def close(self):
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    @staticmethod
    def _directory_flags():
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC

    @classmethod
    def _open_absolute_directory(cls, path: Path):
        descriptor = os.open("/", cls._directory_flags())
        try:
            for part in path.parts[1:]:
                child = os.open(
                    part, cls._directory_flags(), dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _parts(relative):
        if isinstance(relative, (str, os.PathLike)):
            path = Path(relative)
        else:
            path = Path(*relative)
        if path.is_absolute() or any(
            part in {"", ".", ".."} for part in path.parts
        ):
            raise ValueError("AgentWS queue path must stay below its root")
        return path.parts

    def _open_directory(self, relative):
        parts = self._parts(relative)
        descriptor = os.dup(self.root_fd)
        try:
            for part in parts:
                child = os.open(
                    part, self._directory_flags(), dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _open_parent(self, relative):
        parts = self._parts(relative)
        if not parts:
            raise ValueError("AgentWS queue file path is empty")
        return self._open_directory(parts[:-1]), parts[-1]

    def _open_regular(self, relative):
        parent_fd, name = self._open_parent(relative)
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | _CLOEXEC
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(
                    errno.EINVAL,
                    "AgentWS queue entry is not a regular file",
                )
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _entries(self, descriptor):
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if self._entries_seen >= self.max_entries:
                    raise QueueLimitError(
                        errno.EFBIG,
                        f"AgentWS queue scan exceeds {self.max_entries} entries",
                    )
                self._entries_seen += 1
                yield entry

    def has_directory(self, relative):
        try:
            descriptor = self._open_directory(relative)
        except (OSError, ValueError):
            return False
        os.close(descriptor)
        return True

    def require_directory(self, relative):
        descriptor = self._open_directory(relative)
        os.close(descriptor)

    def has_regular_file(self, relative):
        try:
            descriptor = self._open_regular(relative)
        except (OSError, ValueError):
            return False
        os.close(descriptor)
        return True

    def list_directories(self, relative, include_hidden=False):
        try:
            descriptor = self._open_directory(relative)
        except (OSError, ValueError):
            return []
        try:
            result = []
            for entry in self._entries(descriptor):
                if not include_hidden and entry.name.startswith("."):
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_directory:
                    result.append(entry.name)
            return sorted(result)
        finally:
            os.close(descriptor)

    def list_regular_files(self, relative, suffix=""):
        try:
            descriptor = self._open_directory(relative)
        except (OSError, ValueError):
            return []
        try:
            result = []
            for entry in self._entries(descriptor):
                if suffix and not entry.name.endswith(suffix):
                    continue
                try:
                    is_regular = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if is_regular:
                    result.append(entry.name)
            return sorted(result)
        finally:
            os.close(descriptor)

    def directory_timestamp(self, relative):
        try:
            descriptor = self._open_directory(relative)
        except (OSError, ValueError):
            return None
        try:
            modified = os.fstat(descriptor).st_mtime
        finally:
            os.close(descriptor)
        return (
            datetime.fromtimestamp(modified, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def file_size(self, relative):
        try:
            descriptor = self._open_regular(relative)
        except (OSError, ValueError):
            return 0
        try:
            return os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)

    def read_file(self, relative, max_bytes=256 * 1024, tail=False):
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if max_bytes > self.max_file_bytes:
            raise ValueError(
                f"max_bytes exceeds the {self.max_file_bytes}-byte queue limit"
            )
        descriptor = self._open_regular(relative)
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            size = os.fstat(stream.fileno()).st_size
            remaining = self.max_read_bytes - self._bytes_read
            if remaining < 0:
                remaining = 0
            if tail and size > max_bytes:
                stream.seek(max(0, size - max_bytes))
                data = stream.read(min(max_bytes, remaining + 1))
                truncated = True
                prefix = True
            else:
                data = stream.read(min(max_bytes + 1, remaining + 1))
                truncated = size > max_bytes or len(data) > max_bytes
                prefix = False
            if len(data) > remaining:
                raise QueueLimitError(
                    errno.EFBIG,
                    "AgentWS queue reads exceed "
                    f"{self.max_read_bytes} bytes",
                )
            self._bytes_read += len(data)
            data = data[:max_bytes]
        text = data.decode("utf-8", "replace")
        if truncated:
            marker = (
                "\n[... earlier content truncated ...]\n"
                if prefix
                else "\n[... content truncated ...]\n"
            )
            text = marker + text if prefix else text + marker
        return text, size, truncated

    def read_text(
        self,
        relative,
        fallback="",
        max_bytes=256 * 1024,
        tail=False,
    ):
        try:
            return self.read_file(
                relative, max_bytes=max_bytes, tail=tail
            )[0]
        except QueueLimitError:
            raise
        except (OSError, ValueError):
            return fallback

    def fifo_writable(self, relative):
        try:
            parent_fd, name = self._open_parent(relative)
        except (OSError, ValueError):
            return False
        flags = os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW | _CLOEXEC
        try:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISFIFO(entry.st_mode):
                return False
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            try:
                return stat.S_ISFIFO(os.fstat(descriptor).st_mode)
            finally:
                os.close(descriptor)
        except OSError:
            return False
        finally:
            os.close(parent_fd)

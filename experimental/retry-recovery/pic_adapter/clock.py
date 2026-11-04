"""Worker-scoped test clock for the pinned PIC SDK. Test instrumentation only.

The pinned SDK (pic-standard 330fdd81) reads wall time with
datetime.now(timezone.utc) in two modules and offers no clock parameter:

  pic_standard.evidence  attestation expires_at; rejects when expires_at < now
  pic_standard.keyring   key expires_at;         rejects when now >= expires_at

fixed_utc_now() replaces the module-level `datetime` name in exactly those
two modules with a subclass whose now() returns the supplied instant, for
the duration of one verification. Signature, binding and freshness logic
run unchanged and keep their own boundaries; only the instant they compare
against is fixed. This is not a PIC clock API.

The override is process-global. Isolation comes from running verification
in the separate worker subprocess launched by the harness. In addition,
fixed_utc_now() refuses to run until the process has explicitly opted in
with enable_worker_clock(), which only pic_adapter/worker.py calls at
startup, so a clock supplied in any other process fails explicitly instead
of silently falling back to wall time. Not thread-safe: the worker verifies
one request at a time.

Test clocks are whole-second RFC 3339 timestamps with "Z" or an explicit
offset; the whole-seconds restriction is a fixture choice, not an SDK limit.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import importlib
import re
from typing import Iterator

PATCHED_MODULES = ("pic_standard.evidence", "pic_standard.keyring")
# ASCII digits only; offset hours 00-23 and minutes 00-59. Calendar and
# time-of-day ranges are validated by datetime.fromisoformat.
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)

_worker_enabled = False


class ClockError(ValueError):
    """Invalid test clock, or a clock requested before the worker opted in."""


def enable_worker_clock() -> None:
    """Explicit startup opt-in, called only by the verification worker."""
    global _worker_enabled
    _worker_enabled = True


def parse_test_clock(value: object) -> _dt.datetime:
    """Parse a test clock string into an aware UTC datetime, or raise ClockError."""
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        raise ClockError("clock must be an RFC 3339 timestamp in whole seconds with "
                         f"'Z' or an explicit offset, got {value!r}")
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(_dt.timezone.utc)
    except (ValueError, OverflowError) as e:
        raise ClockError(f"invalid clock {value!r}: {type(e).__name__}: {e}") from e


def _frozen_class(instant: _dt.datetime) -> type:
    class FrozenDatetime(_dt.datetime):
        """Standard datetime except now(), which returns the fixed test instant."""

        @classmethod
        def now(cls, tz=None):
            if tz is None:
                raise ClockError("test clock: naive datetime.now() is not supported")
            return instant.astimezone(tz)

        @classmethod
        def utcnow(cls):
            raise ClockError("test clock: datetime.utcnow() is not supported")

        @classmethod
        def today(cls):
            raise ClockError("test clock: datetime.today() is not supported")

    return FrozenDatetime


def clock_is_clear() -> bool:
    """True when every patched module holds the standard datetime class."""
    return all(getattr(importlib.import_module(name), "datetime", None) is _dt.datetime
               for name in PATCHED_MODULES)


@contextlib.contextmanager
def fixed_utc_now(instant: _dt.datetime) -> Iterator[None]:
    """Fix now() in the pinned SDK's evidence and keyring modules for one block.

    Refuses to patch if either module does not currently hold the standard
    datetime class, which detects an existing patch or nested use. It does
    not detect an unexpected SDK revision; the separate SDK-pin check does.
    """
    if not _worker_enabled:
        raise ClockError("a test clock is only applied after the verification worker "
                         "has opted in with enable_worker_clock()")
    if not isinstance(instant, _dt.datetime) or instant.utcoffset() is None:
        raise ClockError("test clock instant must be a timezone-aware datetime")
    modules = [importlib.import_module(name) for name in PATCHED_MODULES]
    for m in modules:
        if getattr(m, "datetime", None) is not _dt.datetime:
            raise ClockError(f"{m.__name__}.datetime is not the standard class; "
                             "refusing to patch (existing patch or nested clock)")
    frozen = _frozen_class(instant.astimezone(_dt.timezone.utc))
    patched = []
    try:
        for m in modules:
            m.datetime = frozen
            patched.append(m)
        yield
    finally:
        for m in patched:
            m.datetime = _dt.datetime

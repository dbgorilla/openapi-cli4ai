"""Runtime settings set once by the root command and read by every layer.

Module globals on purpose: the CLI is a single process handling one command,
and tests set these directly (``_state._verbose_mode = True``).
"""

from __future__ import annotations

_insecure_mode = False
_verbose_mode = False
_timeout_seconds = 60.0
_max_retries = 0
_profile_override: str | None = None


def set_insecure_mode(insecure: bool) -> None:
    global _insecure_mode
    _insecure_mode = insecure


def get_verify_ssl() -> bool:
    return not _insecure_mode

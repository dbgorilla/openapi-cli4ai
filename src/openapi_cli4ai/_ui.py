"""Shared Rich consoles and the verbose logger."""

from __future__ import annotations

from rich.console import Console

from openapi_cli4ai import _state

console = Console()
err_console = Console(stderr=True)


def _verbose(msg: str) -> None:
    """Print a verbose message to stderr if verbose mode is enabled."""
    if _state._verbose_mode:
        err_console.print(f"[dim]> {msg}[/dim]")

"""
Logging shared by the CLI and the MCP server.

All log output goes to stderr: stdout is reserved for command output and, for the
MCP server, the stdio protocol itself.

Levels (verbosity):
    -1  quiet    errors only                    (-q / --quiet)
     0  default  warnings and errors
     1  verbose  + per-file progress            (-v)
     2  debug    + timings, paths, tracebacks   (-vv)

The default can be set with the CODE_INDEXER_VERBOSITY environment variable
(quiet, warning, info/verbose, debug, or -1..2); command-line flags override it.
"""

from __future__ import annotations
import logging
import os
import sys

logger = logging.getLogger("code_indexer")
logger.propagate = False

_LEVELS = {-1: logging.ERROR, 0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}
_group = {"verbose": 0, "quiet": False}
_current = 0


class _Formatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(levelname)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno == logging.INFO and not record.exc_info:
            return record.getMessage()  # progress lines stay clean
        return super().format(record)


def _env_default() -> int:
    raw = os.environ.get("CODE_INDEXER_VERBOSITY", "").strip().lower()
    names = {"quiet": -1, "error": -1, "warning": 0, "info": 1, "verbose": 1, "debug": 2}
    if raw in names:
        return names[raw]
    try:
        return max(-1, min(2, int(raw)))
    except ValueError:
        return 0


def _apply(level: int):
    global _current
    _current = level
    logger.setLevel(_LEVELS[level])


def configure(verbose: int = 0, quiet: bool = False, from_group: bool = False):
    """
    Set the verbosity. Subcommand flags are merged with any flags given before the
    subcommand (`code-indexer -v index .` and `code-indexer index . -v` are equivalent).
    With no flags at all, CODE_INDEXER_VERBOSITY (or the default) applies.
    """
    if from_group:
        _group["verbose"], _group["quiet"] = verbose, quiet
    else:
        verbose = max(verbose, _group["verbose"])
        quiet = quiet or _group["quiet"]
    if quiet:
        _apply(-1)
    elif verbose:
        _apply(min(verbose, 2))
    else:
        _apply(_env_default())


def get_level() -> int:
    return _current


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_Formatter())
logger.addHandler(_handler)
_apply(_env_default())

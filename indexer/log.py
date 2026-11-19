"""
Logging shared by the CLI and the MCP server.

All log output goes to stderr: stdout is reserved for command output and, for the
MCP server, the stdio protocol itself.

Levels (verbosity):
    -1  quiet    errors only                              (-q / --quiet)
     0  default  warnings and errors
     1  verbose  + per-repo / per-branch summaries        (-v)
     2  detail   + per-file progress                      (-vv)
     3  debug    + timings, paths, tracebacks             (--debug)

--debug is its own flag and implies everything below it.

The default can be set with the CODE_INDEXER_VERBOSITY environment variable
(quiet, warning, info/verbose, detail, debug, or -1..3); command-line flags override it.
"""

from __future__ import annotations
import logging
import os
import sys

logger = logging.getLogger("code_indexer")
logger.propagate = False

# Per-file progress sits between INFO (summaries) and DEBUG.
DETAIL = 15
logging.addLevelName(DETAIL, "DETAIL")

_LEVELS = {-1: logging.ERROR, 0: logging.WARNING, 1: logging.INFO, 2: DETAIL, 3: logging.DEBUG}
_group = {"verbose": 0, "quiet": False, "debug": False}
_current = 0


class _Formatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(levelname)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno in (logging.INFO, DETAIL) and not record.exc_info:
            return record.getMessage()  # progress lines stay clean
        return super().format(record)


def _env_default() -> int:
    raw = os.environ.get("CODE_INDEXER_VERBOSITY", "").strip().lower()
    names = {"quiet": -1, "error": -1, "warning": 0, "info": 1, "verbose": 1,
             "detail": 2, "debug": 3}
    if raw in names:
        return names[raw]
    try:
        return max(-1, min(3, int(raw)))
    except ValueError:
        return 0


def _apply(level: int):
    global _current
    _current = level
    logger.setLevel(_LEVELS[level])


def detail(msg, *args, **kwargs):
    """Log per-file progress (shown with -vv or --debug)."""
    logger.log(DETAIL, msg, *args, **kwargs)


def configure(verbose: int = 0, quiet: bool = False, debug: bool = False, from_group: bool = False):
    """
    Set the verbosity. Subcommand flags are merged with any flags given before the
    subcommand (`code-indexer -v index .` and `code-indexer index . -v` are equivalent).
    With no flags at all, CODE_INDEXER_VERBOSITY (or the default) applies.
    """
    if from_group:
        _group["verbose"], _group["quiet"], _group["debug"] = verbose, quiet, debug
    else:
        verbose = max(verbose, _group["verbose"])
        quiet = quiet or _group["quiet"]
        debug = debug or _group["debug"]
    if quiet:
        _apply(-1)
    elif debug:
        _apply(3)
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

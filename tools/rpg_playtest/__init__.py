"""Fixed-seed, offline playtesting for yawn_rpg modules."""

from .simulator import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_STATES,
    DEFAULT_WAIT_MAX,
    GENERIC_ENDINGS,
    SearchConfig,
    SearchResult,
    load_module,
    search_module,
)

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_STATES",
    "DEFAULT_WAIT_MAX",
    "GENERIC_ENDINGS",
    "SearchConfig",
    "SearchResult",
    "load_module",
    "search_module",
]

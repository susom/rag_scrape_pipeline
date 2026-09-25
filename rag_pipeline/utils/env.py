"""
Environment / feature-flag helpers.

Centralizes parsing of boolean environment variables so behavior is consistent
across the web app, the CLI batch job, and the automation orchestrator.
"""

import os

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled", ""}


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable.

    Accepts common truthy/falsy spellings (case-insensitive).
    Invalid values raise rather than silently changing behavior.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean (true/false)")


# Env var that gates ALL mutations of shared SharePoint state (tracker-list
# entries today; source-item status/approval edits in the future). Only the
# "final" environment in a promotion chain (prod later, UAT for now) should set
# this to true, so non-final environments (dev) ingest without telling
# SharePoint a document was already processed.
SHAREPOINT_WRITEBACK_ENABLED = "SHAREPOINT_WRITEBACK_ENABLED"


def sharepoint_writeback_enabled() -> bool:
    """True if this environment may write back to SharePoint.

    Defaults to False so a missing/unset flag never mutates shared SharePoint
    state — an environment must explicitly opt in.
    """
    return env_bool(SHAREPOINT_WRITEBACK_ENABLED, default=False)

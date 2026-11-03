"""API startup dependencies — config validation gate."""

from __future__ import annotations

from collections.abc import Mapping

from chronotrace.config import load_config, validate_startup
from chronotrace.config.exceptions import ChronoTraceConfigError


def get_validated_config(
    config_path: str | None = None,
    env: Mapping[str, str] | None = None,
    check_neo4j: bool = False,
):
    """Load and validate config, raising if not ready.

    Blocks agent/graph init without validated secrets.
    """
    # Loader handles env default, so we pass through
    try:
        config = load_config(config_path=config_path, env=env)
    except ChronoTraceConfigError:
        raise
    # Startup validation gates readiness
    report = validate_startup(config, env=env, check_neo4j=check_neo4j)
    if not report["ok"]:
        raise ChronoTraceConfigError("; ".join(report["errors"]))
    return config, report

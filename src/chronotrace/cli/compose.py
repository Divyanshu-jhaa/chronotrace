"""Helper to locate bundled Compose/Docker assets when installed."""

from __future__ import annotations

import pathlib


def get_compose_path() -> pathlib.Path:
    """Return path to bundled docker-compose.yml, without assuming CWD is repo root."""
    # 1. Try importlib.resources (installed package)
    try:
        from importlib.resources import files  # Python 3.9+

        p = files("chronotrace").joinpath("docker-compose.yml")
        # files() may return a Traversable, try to get as path
        try:
            # Python 3.12: as_file context manager if needed
            import importlib.resources as _res

            if hasattr(p, "is_file") and p.is_file():
                # For package data, try to resolve to real file
                try:
                    # Try to get actual file path via as_file
                    from contextlib import contextmanager

                    # Fallback: check if it's a real file on disk via __file__ location
                    pass
                except Exception:
                    pass
                # Try to return as Path if exists
                try:
                    return pathlib.Path(str(p))
                except Exception:
                    pass
        except Exception:
            pass
        # 2. Check src/chronotrace/docker-compose.yml relative to this file
        candidate = pathlib.Path(__file__).parent.parent / "docker-compose.yml"
        if candidate.exists():
            return candidate
    except Exception:
        pass

    # 2. Relative to this file (src/chronotrace/cli/compose.py -> src/chronotrace/docker-compose.yml)
    candidate = pathlib.Path(__file__).parent.parent / "docker-compose.yml"
    if candidate.exists():
        return candidate

    # 3. Repo root fallback (cwd or parent of this file's ancestors)
    for base in [pathlib.Path.cwd(), pathlib.Path(__file__).parent.parent.parent.parent]:
        cand = base / "docker-compose.yml"
        if cand.exists():
            return cand

    # 4. Absolute fallback to src location
    return pathlib.Path(__file__).parent.parent / "docker-compose.yml"


def get_dockerfile_path() -> pathlib.Path:
    """Return path to bundled Dockerfile."""
    candidate = pathlib.Path(__file__).parent.parent / "Dockerfile"
    if candidate.exists():
        return candidate
    for base in [pathlib.Path.cwd(), pathlib.Path(__file__).parent.parent.parent.parent]:
        cand = base / "Dockerfile"
        if cand.exists():
            return cand
    return candidate

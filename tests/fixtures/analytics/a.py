"""a.py — fixture target file for R-fixture."""
import b  # noqa: F401  # fan-in edge b -> a
value = 42

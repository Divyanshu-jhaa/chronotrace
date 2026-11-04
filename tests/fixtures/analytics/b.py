"""b.py — imports a.py, co-changes with a.py."""
import a  # noqa: F401

def helper():
    return a.value + 1

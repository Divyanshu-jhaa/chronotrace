"""Ensure no module outside config reads YAML/env directly."""

import pathlib
import re


def test_no_yaml_safe_load_outside_config():
    # Search src/chronotrace excluding config/
    root = pathlib.Path("src/chronotrace")
    pattern = re.compile(r"yaml\.safe_load")
    violations = []
    for p in root.rglob("*.py"):
        # Use parts to handle Windows separators
        if "config" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            violations.append(str(p))
    assert violations == [], f"Found yaml.safe_load outside config: {violations}"


def test_no_os_environ_outside_config():
    root = pathlib.Path("src/chronotrace")
    pattern = re.compile(r"os\.environ")
    violations = []
    for p in root.rglob("*.py"):
        if "config" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            violations.append(str(p))
    assert violations == [], f"Found os.environ outside config: {violations}"

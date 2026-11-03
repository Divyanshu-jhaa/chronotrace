"""Test tilde expansion, creation, writability."""

import pathlib

import pytest

from chronotrace.config.exceptions import FilesystemError
from chronotrace.config.loader import _expand_and_ensure_dir


def test_tilde_expansion_posix(tmp_path, monkeypatch):
    # Use tilde path under tmp home — mock expanduser
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(
        pathlib.Path,
        "expanduser",
        lambda self: (
            pathlib.Path(str(self).replace("~", str(fake_home), 1))
            if str(self).startswith("~")
            else self
        ),
    )
    p = _expand_and_ensure_dir("~/mydir", "test")
    assert p.is_absolute()
    assert "mydir" in str(p)
    assert p.exists()


def test_windows_tilde(monkeypatch, tmp_path):
    fake_home = tmp_path / "home_win"
    fake_home.mkdir()
    monkeypatch.setattr(
        pathlib.Path,
        "expanduser",
        lambda self: (
            pathlib.Path(str(self).replace("~", str(fake_home), 1))
            if str(self).startswith("~")
            else self
        ),
    )
    p = _expand_and_ensure_dir("~/.chronotrace/data", "test")
    assert p.exists()


def test_directory_creation(tmp_path):
    target = tmp_path / "new" / "nested" / "dir"
    assert not target.exists()
    p = _expand_and_ensure_dir(str(target), "test")
    assert p.exists()
    assert p.is_dir()


def test_non_writable_via_mock(monkeypatch, tmp_path):
    target = tmp_path / "writable"
    target.mkdir()
    # Mock os.access to return False
    monkeypatch.setattr("chronotrace.config.loader.os.access", lambda p, mode: False)
    with pytest.raises(FilesystemError, match="not writable"):
        _expand_and_ensure_dir(str(target), "test")

    # Mock tempfile to fail
    monkeypatch.setattr("chronotrace.config.loader.os.access", lambda p, mode: True)
    monkeypatch.setattr(
        "chronotrace.config.loader.tempfile.mkstemp",
        lambda dir=None: (_ for _ in ()).throw(  # noqa: E501 - long mock
            PermissionError("denied")
        ),
    )
    with pytest.raises(FilesystemError, match="not writable"):
        _expand_and_ensure_dir(str(target), "test2")

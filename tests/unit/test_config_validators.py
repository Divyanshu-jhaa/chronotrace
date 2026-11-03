"""Test validators helpers."""

import pytest

from chronotrace.config.exceptions import ConfigValidationError
from chronotrace.config.validators import (
    validate_null_or_positive_int,
    validate_positive_float,
    validate_positive_int,
    validate_regex,
)


def test_positive_int_valid():
    assert validate_positive_int(1, "field") == 1
    assert validate_positive_int(500, "batch_size") == 500


def test_positive_int_invalid():
    with pytest.raises(ConfigValidationError, match="must be > 0"):
        validate_positive_int(0, "field")
    with pytest.raises(ConfigValidationError):
        validate_positive_int(-5, "field")
    with pytest.raises(ConfigValidationError):
        validate_positive_int("1", "field")


def test_positive_float_valid():
    assert validate_positive_float(1.5, "decay") == 1.5
    assert validate_positive_float(365, "decay") == 365.0


def test_positive_float_invalid():
    with pytest.raises(ConfigValidationError):
        validate_positive_float(0, "decay")
    with pytest.raises(ConfigValidationError):
        validate_positive_float(-1, "decay")


def test_null_or_positive():
    assert validate_null_or_positive_int(None, "max_commits") is None
    assert validate_null_or_positive_int(10, "max_commits") == 10
    with pytest.raises(ConfigValidationError):
        validate_null_or_positive_int(0, "max_commits")
    with pytest.raises(ConfigValidationError):
        validate_null_or_positive_int(-1, "max_commits")


def test_regex_valid():
    assert validate_regex(r"\bfix\b", "field") == r"\bfix\b"
    assert validate_regex(r"hello.*", "field") == r"hello.*"
    with pytest.raises(ConfigValidationError, match="invalid regex"):
        validate_regex("[unclosed", "field")


def test_regex_invalid():
    with pytest.raises(ConfigValidationError, match="invalid regex"):
        validate_regex("[unclosed", "exclude")

"""Test redaction in config exceptions."""

from chronotrace.config.exceptions import (
    ChronoTraceConfigError,
    ConfigValidationError,
    ConnectivityError,
    FilesystemError,
    MissingSecretError,
)


def test_missing_secret_redaction():
    err = MissingSecretError("MODEL_API_KEY")
    msg = str(err)
    assert "MODEL_API_KEY" in msg
    assert "is required" in msg
    # No secret value leaked
    assert "sk-" not in msg


def test_missing_secret_with_value_not_leaked():
    # Even if caller mistakenly includes value, redaction should hide
    MissingSecretError("NEO4J_PASSWORD")  # noqa: F841
    err2 = MissingSecretError("MODEL_API_KEY", hint="value sk-1234567890 should not appear")
    assert "sk-1234567890" not in str(err2)


def test_config_validation_error_redaction():
    err = ConfigValidationError("MODEL_API_KEY is bad: sk-abcdef1234567890")
    assert "sk-abcdef" not in str(err)


def test_hierarchy():
    assert issubclass(MissingSecretError, ChronoTraceConfigError)
    assert issubclass(ConfigValidationError, ChronoTraceConfigError)
    assert issubclass(FilesystemError, ChronoTraceConfigError)
    assert issubclass(ConnectivityError, ChronoTraceConfigError)


def test_redaction_direct():
    err = ChronoTraceConfigError("password sk-1234567890 should be hidden")
    assert "sk-1234567890" not in str(err)

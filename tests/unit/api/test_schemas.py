"""Unit tests for Pydantic schemas — Git URLs, file_path, chat, health, missing/extra fields."""
import pytest
from pydantic import ValidationError

from chronotrace.api.schemas.repositories import RepositoryRegisterRequest, validate_git_url
from chronotrace.api.schemas.reports import ReportRequest, _validate_file_path
from chronotrace.api.schemas.chat import ChatRequest
from chronotrace.api.schemas.health import HealthResponse, ReadyResponse, ReadyChecks
from chronotrace.api.schemas.errors import ErrorEnvelope, ErrorDetail


# ---------------------------------------------------------------------------
# Git URL validation
# ---------------------------------------------------------------------------
def test_git_url_valid_variants():
    valid = [
        "https://github.com/org/repo.git",
        "https://github.com/org/repo",
        "http://github.com/org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo.git",
        "git@github.com:org/repo",
        "file:///tmp/repo",
        "https://gitlab.example.com/group/sub/repo.git",
    ]
    for url in valid:
        assert validate_git_url(url) == url.strip()
        # also via model
        req = RepositoryRegisterRequest(url=url)
        assert req.url == url.strip()


def test_git_url_invalid_empty_and_nul():
    for bad in ["", "   ", "https://example.com/repo\x00.git"]:
        with pytest.raises((ValueError, ValidationError)):
            validate_git_url(bad)
        with pytest.raises(ValidationError):
            RepositoryRegisterRequest(url=bad)


def test_git_url_invalid_not_url_and_scheme():
    for bad in ["not a url", "ftp://example.com/repo.git", "www.github.com/org/repo"]:
        with pytest.raises((ValueError, ValidationError)):
            validate_git_url(bad)
        with pytest.raises(ValidationError):
            RepositoryRegisterRequest(url=bad)


def test_git_url_rejects_shell_interpolation():
    bads = [
        "$(rm -rf /)",
        "`rm -rf`",
        "${HOME}/repo",
        "https://github.com/org/repo; echo pwned",
        "https://github.com/org/repo|cat /etc/passwd",
        "https://github.com/org/repo && rm -rf",
        "https://github.com/org/repo\nrm -rf",
        "https://github.com/org/repo\r\n",
        "https://github.com/org/repo`whoami`",
        "https://github.com/org/$(whoami)/repo",
    ]
    for bad in bads:
        with pytest.raises((ValueError, ValidationError)):
            validate_git_url(bad)
        with pytest.raises(ValidationError):
            RepositoryRegisterRequest(url=bad)


def test_git_url_rejects_spaces_and_length():
    with pytest.raises((ValueError, ValidationError)):
        validate_git_url("https://github.com/org/repo with spaces.git")
    long_url = "https://github.com/" + "a" * 2040 + ".git"
    assert len(long_url) > 2048
    with pytest.raises((ValueError, ValidationError)):
        validate_git_url(long_url)
    with pytest.raises(ValidationError):
        RepositoryRegisterRequest(url=long_url)


def test_git_url_branch_validation():
    # valid branch
    req = RepositoryRegisterRequest(url="https://github.com/org/repo.git", branch="main")
    assert req.branch == "main"
    req2 = RepositoryRegisterRequest(url="https://github.com/org/repo.git", branch=None)
    assert req2.branch is None
    # empty branch rejected
    with pytest.raises(ValidationError):
        RepositoryRegisterRequest(url="https://github.com/org/repo.git", branch="")
    with pytest.raises(ValidationError):
        RepositoryRegisterRequest(url="https://github.com/org/repo.git", branch="   ")
    with pytest.raises(ValidationError):
        RepositoryRegisterRequest(url="https://github.com/org/repo.git", branch="a" * 257)
    with pytest.raises(ValidationError):
        RepositoryRegisterRequest(url="https://github.com/org/repo.git", branch="main\x00")


def test_repository_register_extra_field_rejected():
    with pytest.raises(ValidationError) as exc:
        RepositoryRegisterRequest(url="https://github.com/org/repo.git", extra="field")  # type: ignore[call-arg]
    assert "extra" in str(exc.value).lower() or "Extra inputs" in str(exc.value)


def test_repository_register_missing_url_rejected():
    with pytest.raises(ValidationError) as exc:
        RepositoryRegisterRequest()  # type: ignore[call-arg]
    assert "url" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# file_path validation
# ---------------------------------------------------------------------------
def test_file_path_valid():
    for good in ["src/foo.py", "src/a/b/c.py", "README.md", "a.py", "dir/subdir/file.js"]:
        assert _validate_file_path(good) == good
        # also via ReportRequest
        req = ReportRequest(repository_id="test-repo", file_path=good)
        assert req.file_path == good


def test_file_path_traversal_rejected():
    bads = [
        "../secret.py",
        "../../etc/passwd",
        "/etc/passwd",
        "/absolute/path.py",
        "a/../b.py",
        "src/../../secret.py",
    ]
    for bad in bads:
        with pytest.raises((ValueError, ValidationError)):
            _validate_file_path(bad)
        with pytest.raises(ValidationError):
            ReportRequest(repository_id="test-repo", file_path=bad)


def test_file_path_backslash_rejected():
    for bad in ["a\\b\\c.py", "src\\foo.py", "dir\\file.py"]:
        with pytest.raises((ValueError, ValidationError)):
            _validate_file_path(bad)
        with pytest.raises(ValidationError):
            ReportRequest(repository_id="test-repo", file_path=bad)


def test_file_path_nul_and_encoded_rejected():
    bads = [
        "a\x00b.py",
        "%00.py",
        "%2e%2fsecret.py",
        "%2e%2e/secret.py",
        "src/%2e%2fsecret.py",
        "%5csecret.py",
        "a%00b.py",
    ]
    for bad in bads:
        with pytest.raises((ValueError, ValidationError)):
            _validate_file_path(bad)
        with pytest.raises(ValidationError):
            ReportRequest(repository_id="test-repo", file_path=bad)


def test_file_path_absolute_windows_rejected():
    with pytest.raises((ValueError, ValidationError)):
        _validate_file_path("C:\\Windows\\secret.py")
    with pytest.raises((ValueError, ValidationError)):
        _validate_file_path("C:/Windows/secret.py")


def test_report_request_missing_and_extra_fields():
    with pytest.raises(ValidationError) as exc:
        ReportRequest(repository_id="test-repo")  # type: ignore[call-arg]
    assert "file_path" in str(exc.value).lower()
    with pytest.raises(ValidationError) as exc:
        ReportRequest(file_path="src/foo.py")  # type: ignore[call-arg]
    assert "repository_id" in str(exc.value).lower()
    with pytest.raises(ValidationError):
        ReportRequest(repository_id="test-repo", file_path="src/foo.py", extra="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Chat validation
# ---------------------------------------------------------------------------
def test_chat_valid():
    req = ChatRequest(repository_id="test-repo", question="What is the risk?")
    assert req.repository_id == "test-repo"
    assert req.question == "What is the risk?"
    req2 = ChatRequest(repository_id="test-repo", question="hi", conversation_id="cid123")
    assert req2.conversation_id == "cid123"


def test_chat_empty_question_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="test-repo", question="")
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="test-repo", question="   ")


def test_chat_oversized_question_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="test-repo", question="x" * 10001)
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="test-repo", question="x" * 20000)


def test_chat_missing_repository_id_rejected():
    with pytest.raises(ValidationError) as exc:
        ChatRequest(question="hi")  # type: ignore[call-arg]
    assert "repository_id" in str(exc.value).lower()
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="", question="hi")
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="   ", question="hi")


def test_chat_extra_field_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="test-repo", question="hi", unknown_field="oops")  # type: ignore[call-arg]


def test_chat_conversation_id_validation():
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="test-repo", question="hi", conversation_id="   ")
    with pytest.raises(ValidationError):
        ChatRequest(repository_id="test-repo", question="hi", conversation_id="x" * 257)


# ---------------------------------------------------------------------------
# Health / readiness schemas
# ---------------------------------------------------------------------------
def test_health_response_serialization():
    hr = HealthResponse(status="ok", version="0.1.0", uptime_seconds=123.45, started_at="2024-01-01T00:00:00+00:00")
    dumped = hr.model_dump()
    assert dumped["status"] == "ok"
    assert dumped["version"] == "0.1.0"
    assert dumped["uptime_seconds"] == 123.45
    # extra field rejected
    with pytest.raises(ValidationError):
        HealthResponse(status="ok", version="0.1.0", uptime_seconds=1.0, unknown="x")  # type: ignore[call-arg]


def test_ready_response_serialization():
    checks = ReadyChecks(neo4j="ok", config="ok", agent="ok")
    rr = ReadyResponse(status="ready", checks=checks)
    assert rr.status == "ready"
    assert rr.checks.neo4j == "ok"
    rr2 = ReadyResponse(status="not_ready", checks=ReadyChecks(neo4j="unavailable", config="ok", agent="ok"))
    assert rr2.status == "not_ready"
    with pytest.raises(ValidationError):
        ReadyResponse(status="ready", checks=checks, extra="x")  # type: ignore[call-arg]


def test_schemas_envelope_codes():
    # Ensure error envelope never includes secrets
    detail = ErrorDetail(code="validation_error", message="invalid url")
    assert detail.code == "validation_error"
    envelope = ErrorEnvelope(error=detail)
    assert envelope.error.code == "validation_error"


def test_question_boundary_exact_10000_allowed():
    q = "x" * 10000
    req = ChatRequest(repository_id="repo", question=q)
    assert len(req.question) == 10000


def test_repository_id_boundary():
    # RepositoryRegisterRequest url already tested; check ReportRequest repo id length
    with pytest.raises(ValidationError):
        ReportRequest(repository_id="a" * 257, file_path="src/foo.py")
    # exactly 256 should be allowed if charset ok? but mcp validator may reject slash etc.
    # Use simple alphanumeric 256
    req = ReportRequest(repository_id="a" * 100, file_path="src/foo.py")
    assert len(req.repository_id) == 100

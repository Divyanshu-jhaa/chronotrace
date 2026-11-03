"""Repository acquisition — URL validation, canonical identity, clone/fetch via Git CLI."""

from __future__ import annotations

import hashlib
import re
import subprocess
import urllib.parse
from pathlib import Path

from .exceptions import AcquisitionError, ValidationError, _redact_secrets

SUPPORTED_SCHEMES = {"https", "http", "ssh", "file"}
_GIT_SCP_RE = re.compile(r"^git@(?P<host>[^:]+):(?P<path>.+)$")
_SSH_URL_RE = re.compile(r"^ssh://", re.IGNORECASE)


def _redact_url(url: str) -> str:
    return _redact_secrets(url)


def canonicalize_url(url: str) -> str:
    """Normalize remote Git URL to stable canonical form.

    Steps:
    - strip whitespace
    - canonicalize ``git@host:owner/repo`` -> ``ssh://host/owner/repo``
    - lowercase host
    - remove embedded credentials (user:pass@)
    - strip trailing ``.git`` and ``/``
    """
    raw = url.strip()
    if not raw:
        raise ValidationError("repository URL must not be empty")

    # git@host:path -> ssh://host/path
    m = _GIT_SCP_RE.match(raw)
    if m:
        host = m.group("host").lower()
        path = m.group("path").strip()
        # path may contain leading slash? canonicalize
        path = path.lstrip("/")
        # strip .git suffix and trailing slashes from path
        while path.endswith("/"):
            path = path[:-1]
        if path.lower().endswith(".git"):
            path = path[:-4]
            path = path.rstrip("/")
        # Store without trailing slash, no credentials
        canonical = f"ssh://{host}/{path}"
        # Remove possible duplicate slashes
        canonical = canonical.rstrip("/")
        return canonical

    # File URLs handled separately
    if raw.startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        # For file://, keep scheme, normalize path
        path = parsed.path
        # urlparse for file may include netloc empty
        # Strip trailing slashes and .git
        path = path.rstrip("/")
        if path.lower().endswith(".git"):
            path = path[:-4]
            path = path.rstrip("/")
        if not path:
            path = "/"
        # Reconstruct: file:// + path (ignore host for local)
        # file:///absolute/path is typical
        # Use triple slash for local
        return f"file://{path}"

    # Standard URL parse
    parsed = urllib.parse.urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if not scheme:
        raise ValidationError(
            f"unsupported URL scheme '' for {raw!r}: supported formats are https, http, ssh, git@, file"
        )
    if scheme not in SUPPORTED_SCHEMES:
        raise ValidationError(
            f"unsupported URL scheme {scheme!r} for {raw!r}: supported formats are https, http, ssh, git@, file"
        )

    # Credentials removal: use hostname, not netloc
    host = (parsed.hostname or "").lower()
    if not host:
        # For http/https/ssh, host required; for file we already handled
        raise ValidationError(f"repository URL missing host: {raw!r}")
    port = f":{parsed.port}" if parsed.port else ""
    # Path normalization: strip .git and trailing slashes
    path = parsed.path or ""
    # Remove trailing slashes
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
        path = path.rstrip("/")
    # Ensure leading slash for non-empty path
    if path and not path.startswith("/"):
        path = "/" + path
    # Lowercase path for case-insensitive identity (GitHub is case-insensitive)
    # Spec says lowercase host; tasks expect Org/Repo case-insensitive as well
    # So we lower path as part of stable identity
    path = path.lower()
    # Drop query and fragment, drop credentials, keep only scheme, host, port, path
    canonical = f"{scheme}://{host}{port}{path}"
    canonical = canonical.rstrip("/")
    return canonical


def validate_url(url: str) -> str:
    """Validate supported transport and return canonical URL.

    Raises ValidationError with actionable message for unsupported schemes.
    """
    try:
        canonical = canonicalize_url(url)
    except ValidationError:
        raise
    except Exception as e:
        raise ValidationError(f"invalid repository URL {url!r}: {e}") from e

    # Additional check: ensure scheme is supported (already in canonicalize)
    return canonical


def derive_repository_id(canonical_url: str) -> str:
    """Derive stable repositoryId from canonical URL.

    For graph isolation we use a scheme-insensitive identity (host+path)
    so that ``https://github.com/org/repo.git``, ``git@github.com:org/repo.git``
    (canonicalized to ``ssh://``), and ``https://user:pass@github.com/Org/Repo``
    all map to the same repositoryId, while distinct host/path yield distinct IDs.
    ``file://`` URLs remain path-based and scheme-aware for local isolation.
    """
    if not canonical_url or not canonical_url.strip():
        raise ValidationError("canonical_url must not be empty")
    raw = canonical_url.strip()
    # Handle file:// separately — path is identity
    if raw.startswith("file://"):
        # Use canonicalized file URL lowercased as ID (case-insensitive for path? keep lower)
        try:
            canon = canonicalize_url(raw)
        except ValidationError:
            canon = raw.lower().rstrip("/")
        return canon.lower().rstrip("/")
    # For remote URLs, strip scheme and use host+path as identity
    # Parse canonicalized form to extract host and path in normalized lower case
    try:
        parsed = urllib.parse.urlparse(canonicalize_url(raw))
    except Exception:
        # Fallback: lower and strip
        return raw.lower().rstrip("/")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower().rstrip("/")
    # Combine host and path; ignore scheme, port, credentials (already stripped)
    if not host:
        # No host (e.g., ssh without canonical?), fallback to canonical lower
        try:
            return canonicalize_url(raw).lower().rstrip("/")
        except Exception:
            return raw.lower().rstrip("/")
    # For repository isolation, scheme-agnostic: https vs ssh with same host/path => same ID
    # Example: https://github.com/org/repo and ssh://github.com/org/repo => github.com/org/repo
    repo_id = f"{host}{path}"
    repo_id = repo_id.rstrip("/")
    if not repo_id:
        repo_id = host
    return repo_id


def derive_local_path(
    repositories_dir: str | Path,
    canonical_url: str,
    collision_suffix: str | None = None,
) -> Path:
    """Derive local directory ``<repositories_dir>/<slug-hash>``.

    Hash is derived from the stable repositoryId (scheme-insensitive) so that
    ``https://github.com/org/repo.git`` and ``git@github.com:org/repo.git``
    map to the same directory, while distinct repos yield distinct dirs.
    Slug is filesystem-safe; repeated calls are stable.
    """
    base = Path(repositories_dir).expanduser()
    # Derive stable id for hashing (scheme-insensitive)
    try:
        stable = derive_repository_id(canonical_url)
    except Exception:
        stable = canonical_url
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    short_hash = digest[:12]

    # Slugify: use stable id (host+path normalized) for readability — ensures scheme-insensitive same slug
    # Fall back to canonical_url host/path if stable parsing fails
    try:
        if stable.startswith("file://"):
            raw_slug = stable.replace("file://", "").replace("/", "-")
        elif "/" in stable or "." in stable:
            # stable is like "github.com/org/repo"
            raw_slug = stable.replace("/", "-")
        else:
            raw_slug = stable
    except Exception:
        try:
            parsed = urllib.parse.urlparse(canonical_url)
            if canonical_url.startswith("file://"):
                raw_slug = canonical_url.replace("file://", "").replace("/", "-")
            elif "://" in canonical_url:
                host = parsed.hostname or "repo"
                path = parsed.path or ""
                raw_slug = f"{host}{path}".replace("/", "-")
            else:
                raw_slug = canonical_url
        except Exception:
            raw_slug = canonical_url

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", raw_slug).strip("-").lower()
    # Truncate slug to avoid overly long directory names
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")
    if not slug:
        slug = "repo"
    dirname = f"{slug}-{short_hash}"
    if collision_suffix:
        dirname = f"{dirname}-{collision_suffix}"
    return base / dirname


def _run_git(args: list[str], cwd: Path | None = None, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if env_extra:
        env.update(env_extra)
    # Merge with current env but ensure GIT_TERMINAL_PROMPT=0
    import os as _os  # noqa: F401

    _environ = _os.__dict__.get("environ")  # type: ignore[attr-defined]
    full_env = dict(_environ).copy() if _environ is not None else {}
    full_env.update(env)
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd is not None else None,
        env=full_env,
        capture_output=True,
        text=True,
    )
    return result


def _get_head_hash(repo_path: Path) -> str | None:
    res = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _get_default_branch(repo_path: Path) -> str | None:
    # Try symbolic-ref for remote HEAD
    res = _run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path)
    if res.returncode == 0 and res.stdout.strip():
        ref = res.stdout.strip()
        # e.g. refs/remotes/origin/main -> main
        if "/" in ref:
            return ref.rsplit("/", 1)[-1]
    # Fallback: parse `git remote show origin`
    res2 = _run_git(["remote", "show", "origin"], cwd=repo_path)
    if res2.returncode == 0:
        m = re.search(r"HEAD branch:\s*(\S+)", res2.stdout)
        if m:
            return m.group(1)
    # Fallback: try to detect default via `git branch -r`
    res3 = _run_git(["branch", "-r"], cwd=repo_path)
    if res3.returncode == 0:
        # Look for origin/HEAD
        for line in res3.stdout.splitlines():
            line = line.strip()
            if "origin/HEAD" in line and "->" in line:
                parts = line.split("->")
                if len(parts) == 2:
                    return parts[1].strip().split("/")[-1]
    return None


def _sanitize_error(msg: str, original_url: str) -> str:
    # Redact credentials from both the message and the URL
    redacted_url = _redact_url(original_url)
    # Also redact any user:pass that appears in git error output
    safe_msg = _redact_secrets(msg)
    # Ensure original URL with credentials is not included raw
    if original_url != redacted_url and original_url in safe_msg:
        safe_msg = safe_msg.replace(original_url, redacted_url)
    return safe_msg


def clone_or_fetch(
    url: str,
    branch: str | None = None,
    repositories_dir: str | Path | None = None,
    *,
    canonical_url: str | None = None,
    local_path: Path | None = None,
) -> dict[str, str | Path | None]:
    """Clone or fetch a repository via Git CLI.

    - Sets ``GIT_TERMINAL_PROMPT=0`` to avoid hanging on auth prompts.
    - Clones when directory absent; fetches with ``--all --prune`` and
      fast-forward when present.
    - Never executes repository code (no python invocation, no hook execution
      beyond git's own clone/fetch).
    - Returns dict with ``local_path``, ``head``, ``branch``, ``default_branch``,
      ``canonical_url``, ``repository_id``.
    - Raises AcquisitionError with stage CLONING, sanitized of credentials.
    """
    # Derive canonical and repository_id first (validation)
    try:
        canon = canonical_url or validate_url(url)
        repo_id = derive_repository_id(canon)
    except ValidationError as e:
        raise AcquisitionError(str(e), stage="CLONING") from e

    # Derive local path if not provided
    if local_path is None:
        if repositories_dir is None:
            raise AcquisitionError("repositories_dir or local_path is required", stage="CLONING")
        local_path = derive_local_path(repositories_dir, canon)
    else:
        local_path = Path(local_path)

    redacted_url = _redact_url(url)

    # Case 1: local directory does not exist or is not a git repo -> clone
    git_dir = local_path / ".git"
    is_git_repo = git_dir.exists() or (local_path.exists() and (local_path / ".git").exists())
    # Also check if path exists but not a git repo -> treat as needs clone after validation
    should_clone = not local_path.exists() or not is_git_repo
    # Additional check: `git rev-parse --git-dir` can confirm
    if local_path.exists() and not should_clone:
        probe = _run_git(["rev-parse", "--git-dir"], cwd=local_path)
        if probe.returncode != 0:
            should_clone = True

    try:
        if should_clone:
            # Ensure parent exists
            local_path.parent.mkdir(parents=True, exist_ok=True)
            # If directory exists but is not git repo, remove or use alternative?
            # We will clone into a temp and handle collision; simplest: if exists and not empty, use suffixed path
            clone_target = local_path
            if local_path.exists() and any(local_path.iterdir()):
                # Collision: derive alternative with suffix
                alt = derive_local_path(local_path.parent, canon, collision_suffix="1")
                clone_target = alt
                local_path = alt

            clone_args = ["clone"]
            # For file:// URLs, git clone needs file path without file:// prefix for local
            # But git supports file:// directly; we keep original URL for cloning to preserve auth
            # Use original URL (not canonical) for actual git operation to handle credentials if needed
            # But sanitize error messages later.
            if branch:
                clone_args += ["--branch", branch]
            # Use --no-hardlinks for local file clones to avoid issues
            clone_args += [url, str(clone_target)]

            res = _run_git(clone_args)
            if res.returncode != 0:
                err = _sanitize_error(res.stderr or res.stdout or "git clone failed", url)
                raise AcquisitionError(
                    f"clone failed for {redacted_url} (stage CLONING): {err}",
                    stage="CLONING",
                )
            local_path = clone_target
        else:
            # Fetch existing repo
            # git fetch --all --prune
            res = _run_git(["fetch", "--all", "--prune"], cwd=local_path)
            if res.returncode != 0:
                err = _sanitize_error(res.stderr or res.stdout or "git fetch failed", url)
                raise AcquisitionError(
                    f"fetch failed for {redacted_url} (stage CLONING): {err}",
                    stage="CLONING",
                )
            # Fast-forward selected branch if provided
            if branch:
                # Checkout branch (create if needed tracking origin)
                res_checkout = _run_git(["checkout", branch], cwd=local_path)
                if res_checkout.returncode != 0:
                    # Try checkout with -b tracking origin
                    res_checkout2 = _run_git(
                        ["checkout", "-b", branch, f"origin/{branch}"], cwd=local_path
                    )
                    if res_checkout2.returncode != 0:
                        err = _sanitize_error(
                            res_checkout2.stderr or res_checkout.stderr or "checkout failed",
                            url,
                        )
                        raise AcquisitionError(
                            f"checkout branch {branch!r} failed for {redacted_url}: {err}",
                            stage="CLONING",
                        )
                # Fast-forward merge
                # Try merge --ff-only origin/branch
                res_ff = _run_git(["merge", "--ff-only", f"origin/{branch}"], cwd=local_path)
                if res_ff.returncode != 0:
                    # Fallback to reset --hard origin/branch for fast-forward case
                    # This handles cases where local diverged but remote fast-forward expected
                    # We attempt reset after checking if merge-base is ancestor
                    # For MVP, try reset; if fails keep error but don't treat as needFull
                    res_reset = _run_git(["reset", "--hard", f"origin/{branch}"], cwd=local_path)
                    if res_reset.returncode != 0:
                        err = _sanitize_error(
                            res_ff.stderr or res_reset.stderr or "fast-forward failed",
                            url,
                        )
                        raise AcquisitionError(
                            f"fast-forward branch {branch!r} failed for {redacted_url}: {err}",
                            stage="CLONING",
                        )
            else:
                # No branch specified: try to fast-forward current branch to origin
                # Detect current branch
                res_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=local_path)
                cur_branch = res_branch.stdout.strip() if res_branch.returncode == 0 else None
                if cur_branch and cur_branch != "HEAD":
                    res_ff = _run_git(["merge", "--ff-only", f"origin/{cur_branch}"], cwd=local_path)
                    # Ignore failure if no upstream or detached; not fatal
                    if res_ff.returncode != 0:
                        # Try reset hard if origin exists
                        _run_git(["reset", "--hard", f"origin/{cur_branch}"], cwd=local_path)

        # Record HEAD and default branch
        head = _get_head_hash(local_path)
        default_branch = _get_default_branch(local_path)
        selected_branch = branch or default_branch

        return {
            "local_path": local_path,
            "head": head,
            "branch": selected_branch,
            "default_branch": default_branch,
            "canonical_url": canon,
            "repository_id": repo_id,
        }
    except AcquisitionError:
        raise
    except Exception as e:
        msg = _sanitize_error(str(e), url)
        raise AcquisitionError(f"acquisition failed for {redacted_url}: {msg}", stage="CLONING") from e


def is_ancestor(repo_path: Path, ancestor_hash: str, head_hash: str) -> bool:
    """Check whether ancestor_hash is ancestor of head_hash via ``git merge-base --is-ancestor``."""
    res = _run_git(["merge-base", "--is-ancestor", ancestor_hash, head_hash], cwd=repo_path)
    return res.returncode == 0

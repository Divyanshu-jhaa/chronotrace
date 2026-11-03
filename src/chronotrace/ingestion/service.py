"""Ingestion orchestration — status machine, incremental ingestion, batched writes."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .acquisition import clone_or_fetch, derive_repository_id, is_ancestor, validate_url
from .exceptions import AcquisitionError, IngestionError, RewrittenHistoryError, _redact_secrets
from .extractor import extract_commits
from .filtering import filter_commits
from .labeling import label_commit

logger = logging.getLogger(__name__)


class IngestionStatus(str, Enum):
    PENDING = "PENDING"
    CLONING = "CLONING"
    EXTRACTING = "EXTRACTING"
    FILTERING = "FILTERING"
    WRITING_GRAPH = "WRITING_GRAPH"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(slots=True)
class IngestionState:
    repository_id: str
    canonical_url: str
    status: IngestionStatus = IngestionStatus.PENDING
    stage_timestamps: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    error_summary: str | None = None
    last_ingested_head: str | None = None
    last_successful_at: str | None = None
    needs_full_reingestion: bool = False
    current_head: str | None = None
    default_branch: str | None = None
    local_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "canonical_url": self.canonical_url,
            "status": self.status.value if isinstance(self.status, Enum) else str(self.status),
            "stage_timestamps": dict(self.stage_timestamps),
            "counters": dict(self.counters),
            "error_summary": self.error_summary,
            "last_ingested_head": self.last_ingested_head,
            "last_successful_at": self.last_successful_at,
            "needs_full_reingestion": self.needs_full_reingestion,
            "current_head": self.current_head,
            "default_branch": self.default_branch,
            "local_path": str(self.local_path) if self.local_path else None,
        }


@dataclass(slots=True)
class IngestionResult:
    state: IngestionState
    commits_processed: int
    files_processed: int
    filtered_out: int
    batches: int


# In-memory status store (per-process). For graph persistence we also upsert Repository node.
_STATUS_STORE: dict[str, IngestionState] = {}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _transition(state: IngestionState, new_status: IngestionStatus) -> None:
    state.status = new_status
    state.stage_timestamps[new_status.value] = _now_iso()
    logger.info("Ingestion %s -> %s", state.repository_id, new_status.value)


def _redact(msg: str) -> str:
    return _redact_secrets(msg)


def _get_graph_head(gateway: Any, repository_id: str) -> str | None:
    """Read lastIngestedHead from graph if available."""
    if gateway is None:
        return None
    try:
        repo = gateway.get_repository(repository_id)
        if repo is None:
            return None
        # Try multiple possible field names for head
        head = getattr(repo, "current_head", None) or getattr(repo, "last_ingested_head", None)
        if head:
            return str(head)
        # Fallback: try raw dict via direct query? Use gateway driver session if available
        # Attempt to query Repository node directly via driver
        try:
            driver = getattr(gateway, "driver", None) or getattr(gateway, "_driver", None)
            if driver is not None:
                with driver.session(database=driver.database) as session:
                    result = session.run(
                        "MATCH (r:Repository {id: $id}) RETURN r.lastIngestedHead as head, r.current_head as cur",
                        id=repository_id,
                    )
                    record = result.single()
                    if record:
                        return record["head"] or record["cur"]  # type: ignore[no-any-return]
        except Exception:
            pass
        return None
    except Exception as e:
        logger.debug("failed to read lastIngestedHead: %s", e)
        return None


def _persist_head(gateway: Any, repository_id: str, head: str, default_branch: str | None, canonical_url: str) -> None:
    if gateway is None:
        return
    try:
        from chronotrace.graph.models import Repository

        repo = Repository.create(
            name=repository_id,
            remote_url=canonical_url,
            repository_id=repository_id,
            default_branch=default_branch,
            current_head=head,
            last_ingested_at=_now_iso(),
        )
        gateway.upsert_repository(repo)
        # Also set lastIngestedHead property explicitly via Cypher for incremental logic
        try:
            driver = getattr(gateway, "driver", None) or getattr(gateway, "_driver", None)
            if driver is not None:
                with driver.session(database=driver.database) as session:
                    session.run(
                        """
                        MATCH (r:Repository {id: $id})
                        SET r.lastIngestedHead = $head,
                            r.lastSuccessfulIngestionAt = datetime(),
                            r.currentHead = $head
                        """,
                        id=repository_id,
                        head=head,
                    ).consume()
        except Exception as ee:
            logger.debug("failed to set lastIngestedHead via Cypher: %s", ee)
    except Exception as e:
        logger.warning("failed to persist repository head: %s", _redact(str(e)))


def _get_new_hashes(repo_path: Path, last_head: str | None) -> list[str] | None:
    """Get commit hashes after last_head via git rev-list, or None if not determinable."""
    if not last_head:
        return None
    # Check if last_head exists in repo
    res_exists = subprocess.run(
        ["git", "cat-file", "-e", last_head],
        cwd=str(repo_path),
        capture_output=True,
        env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if res_exists.returncode != 0:
        return None
    # Get current HEAD
    res_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if res_head.returncode != 0:
        return None
    head = res_head.stdout.strip()
    if not head:
        return None
    # git rev-list last_head..HEAD
    res = subprocess.run(
        ["git", "rev-list", f"{last_head}..HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if res.returncode != 0:
        # Maybe last_head is not ancestor; we handle via merge-base elsewhere
        return None
    hashes = [h.strip() for h in res.stdout.splitlines() if h.strip()]
    # Reverse to chronological? rev-list is reverse chronological; we want chronological order as per extraction
    hashes.reverse()
    return hashes


class IngestionService:
    """Orchestrates acquisition, extraction, filtering, labeling, and graph writes.

    - Status machine PENDING -> CLONING -> EXTRACTING -> FILTERING -> WRITING_GRAPH -> COMPLETED (FAILED from any)
    - Incremental ingestion via lastIngestedHead and merge-base ancestor check
    - Batched parameterized UNWIND writes via GraphGateway
    - Co-change edge recomputation
    - Observability via logging and counters
    """

    def __init__(self, config: Any | None = None, gateway: Any | None = None, env: Any | None = None) -> None:
        self.config = config
        self.gateway = gateway
        self.env = env
        # Resolve config values with defaults
        if config is not None:
            try:
                self.repositories_dir = Path(getattr(getattr(config, "project", None), "repositories_dir", "~/.chronotrace/repositories")).expanduser()
            except Exception:
                self.repositories_dir = Path("~/.chronotrace/repositories").expanduser()
            try:
                self.batch_size = int(getattr(getattr(config, "graph", None), "batch_size", 500))
            except Exception:
                self.batch_size = 500
            try:
                filtering = getattr(getattr(config, "analysis", None), "filtering", None)
                self.exclusion_patterns: list[str] = list(getattr(filtering, "exclude", []) or [])
            except Exception:
                self.exclusion_patterns = []
            try:
                bugfix = getattr(getattr(config, "analysis", None), "bug_fix_labels", None)
                self.fix_patterns: list[str] = list(getattr(bugfix, "patterns", []) or [])
                self.case_sensitive: bool = bool(getattr(bugfix, "case_sensitive", False))
            except Exception:
                self.fix_patterns = []
                self.case_sensitive = False
            try:
                self.max_commits = getattr(getattr(getattr(config, "analysis", None), "history", None), "max_commits", None)
            except Exception:
                self.max_commits = None
        else:
            self.repositories_dir = Path("~/.chronotrace/repositories").expanduser()
            self.batch_size = 500
            self.exclusion_patterns = []
            self.fix_patterns = []
            self.case_sensitive = False
            self.max_commits = None

    def get_status(self, repository_id: str) -> IngestionState | None:
        # Check in-memory store first
        if repository_id in _STATUS_STORE:
            return _STATUS_STORE[repository_id]
        # Try graph
        if self.gateway is not None:
            try:
                # Attempt to reconstruct state from graph Repository node
                repo = self.gateway.get_repository(repository_id)
                if repo is not None:
                    state = IngestionState(
                        repository_id=repository_id,
                        canonical_url=getattr(repo, "remote_url", repository_id) or repository_id,
                        status=IngestionStatus.COMPLETED if getattr(repo, "current_head", None) else IngestionStatus.PENDING,
                        stage_timestamps={},
                        counters={},
                        last_ingested_head=getattr(repo, "current_head", None),
                        last_successful_at=getattr(repo, "last_ingested_at", None),
                    )
                    return state
            except Exception:
                pass
        return None

    def ingest(
        self,
        url: str,
        branch: str | None = None,
        *,
        repositories_dir: str | Path | None = None,
    ) -> IngestionResult:
        """Run full ingestion pipeline for a repository URL."""
        # Validate and canonicalize
        try:
            canonical = validate_url(url)
            repository_id = derive_repository_id(canonical)
        except Exception as e:
            # Validation failure -> FAILED state
            state = IngestionState(
                repository_id=url,
                canonical_url=url,
                status=IngestionStatus.FAILED,
                error_summary=_redact(str(e)),
            )
            state.stage_timestamps[IngestionStatus.FAILED.value] = _now_iso()
            state.stage_timestamps[IngestionStatus.PENDING.value] = _now_iso()
            _STATUS_STORE[url] = state
            raise

        repos_dir = Path(repositories_dir).expanduser() if repositories_dir else self.repositories_dir
        state = IngestionState(
            repository_id=repository_id,
            canonical_url=canonical,
            status=IngestionStatus.PENDING,
            counters={"commits": 0, "files": 0, "filteredOut": 0, "batches": 0},
        )
        state.stage_timestamps[IngestionStatus.PENDING.value] = _now_iso()
        _STATUS_STORE[repository_id] = state
        _STATUS_STORE[canonical] = state

        current_head: str | None = None
        local_path: Path | None = None
        default_branch: str | None = None
        last_head: str | None = None

        try:
            # Read lastIngestedHead for incremental logic (before cloning)
            try:
                last_head = _get_graph_head(self.gateway, repository_id)
            except Exception:
                last_head = None
            if last_head:
                state.last_ingested_head = last_head

            # --- CLONING ---
            _transition(state, IngestionStatus.CLONING)
            acq = clone_or_fetch(url, branch=branch, repositories_dir=repos_dir, canonical_url=canonical)
            local_path = Path(acq["local_path"])  # type: ignore[arg-type]
            state.local_path = local_path
            current_head = acq.get("head")  # type: ignore[assignment]
            state.current_head = current_head
            default_branch = acq.get("default_branch")  # type: ignore[assignment]
            state.default_branch = default_branch
            selected_branch = acq.get("branch")  # type: ignore[assignment]
            # Persist Repository node early with current head (but not lastIngestedHead until success)
            if self.gateway is not None:
                try:
                    from chronotrace.graph.models import Repository

                    repo_node = Repository.create(
                        name=repository_id,
                        remote_url=canonical,
                        repository_id=repository_id,
                        default_branch=default_branch,
                        current_head=current_head,
                    )
                    self.gateway.upsert_repository(repo_node)
                except Exception as e:
                    logger.debug("repository upsert during cloning failed: %s", _redact(str(e)))

            # Rewritten-history detection if we have a prior head
            if last_head and current_head and last_head != current_head:
                # Check ancestor via git merge-base --is-ancestor
                try:
                    is_anc = is_ancestor(local_path, last_head, current_head)
                except Exception:
                    is_anc = True  # fail open? but we treat as not rewritten if check fails
                if not is_anc:
                    state.needs_full_reingestion = True
                    raise RewrittenHistoryError(
                        f"rewritten history detected: lastIngestedHead {last_head[:8]} is not ancestor of HEAD {current_head[:8]}; needsFullReingestion=true",
                    )

            # --- EXTRACTING ---
            _transition(state, IngestionStatus.EXTRACTING)
            # Determine from_commit for incremental: use last_head if incremental
            from_commit = last_head if last_head else None
            # Extract commits chronologically
            commits = extract_commits(
                repo_path=local_path,
                branch=branch or selected_branch,  # type: ignore[arg-type]
                from_commit=from_commit,
                max_commits=self.max_commits,
            )
            # Filter to only new commits after last_head if incremental (exclusive)
            if last_head:
                # Build set of hashes after last_head via git rev-list
                new_hashes = _get_new_hashes(local_path, last_head)
                if new_hashes is not None:
                    new_set = set(new_hashes)
                    # Keep only commits whose hash in new_set
                    commits = [c for c in commits if c.hash.lower() in new_set]
                    # If no new commits, we still succeed with 0 processed
                    if not commits:
                        logger.info("no new commits after %s", last_head[:8])
                else:
                    # Fallback: remove the last_head commit itself if it was included inclusively
                    commits = [c for c in commits if c.hash.lower() != last_head.lower()]
                    # Also handle case where commits are not in order relative to last_head:
                    # Keep only commits that are descendants? Without new_hashes we keep filtered list
                    # Trust extractor with from_commit gave us tail

            # Apply fix/revert labeling to commits now (store in memory)
            labeled: list[tuple[Any, Any]] = []  # (commit, labelingResult)
            for c in commits:
                lr = label_commit(c.message, self.fix_patterns, self.case_sensitive)
                labeled.append((c, lr))
            state.counters["commits"] = len(commits)

            # --- FILTERING ---
            _transition(state, IngestionStatus.FILTERING)
            filtering_result = filter_commits(commits, self.exclusion_patterns)
            filtered_commits_labels: list[tuple[Any, Any]] = []
            # Need to map filtered commits back to labeling results
            # Filtering returns new CommitData objects with pruned modifications; maintain labeling
            hash_to_label: dict[str, Any] = {c.hash: lr for c, lr in labeled}
            for fc in filtering_result.filtered_commits:
                lr2: Any = hash_to_label.get(fc.hash)
                # Re-label after filtering? message same, so reuse
                if lr2 is None:
                    lr2 = label_commit(fc.message, self.fix_patterns, self.case_sensitive)
                filtered_commits_labels.append((fc, lr2))
            state.counters["filteredOut"] = filtering_result.filtered_out_count
            state.counters["files"] = filtering_result.kept_count  # approx files count

            # --- WRITING_GRAPH ---
            _transition(state, IngestionStatus.WRITING_GRAPH)

            # Prepare graph writes if gateway available
            total_batches = 0
            if self.gateway is not None and filtered_commits_labels:
                # Build unique authors, commits, files
                from chronotrace.graph.models import Author, Commit, File, derive_author_id, derive_commit_id, derive_file_id
                from chronotrace.graph.models import normalize_path as graph_normalize

                # Authors deduplication
                author_map: dict[str, Author] = {}
                commit_models: list[Commit] = []
                file_map: dict[str, File] = {}
                # For alive flag handling and rename
                # Track file state across commits in order
                # We maintain a dict of current files by repository-scoped path
                current_files: dict[str, File] = {}

                # Pre-load existing files for rename handling if incremental?
                # For MVP, start fresh map; incremental will merge via upsert

                for fc, lr in filtered_commits_labels:
                    # Author
                    email = fc.author_email
                    name = fc.author_name
                    try:
                        aid = derive_author_id(repository_id, email, name)
                    except ValueError:
                        # Missing author identity: use fallback hash of commit hash
                        aid = f"{repository_id}:author:{hashlib.sha256(fc.hash.encode()).hexdigest()[:12]}"
                        # Create author with whatever name/email available
                    if aid not in author_map:
                        author_map[aid] = Author.create(repository_id=repository_id, name=name, email=email, author_id=aid)

                    # Commit model with labeling
                    commit_id = derive_commit_id(repository_id, fc.hash)
                    # Normalize date to iso string if needed
                    date_val = fc.committer_date
                    if hasattr(date_val, "isoformat"):
                        date_str = date_val.isoformat()  # type: ignore[union-attr]
                    elif date_val is not None:
                        date_str = str(date_val)
                    else:
                        date_str = None  # type: ignore[assignment]
                    commit_model = Commit.create(
                        repository_id=repository_id,
                        hash=fc.hash,
                        date=date_str,
                        message=fc.message,
                        is_fix=lr.is_fix,
                        is_revert=lr.is_revert,
                        changed_file_count=len(fc.modifications),
                        author_id=aid,
                    )
                    # Attach fixReason as extra prop for persistence via direct props?
                    # Commit.to_props doesn't include fixReason, but we can handle via gateway execute_batch with extra prop
                    # For now store fixReason in Commit model's extra handling via direct batch
                    # We'll collect fixReason separately
                    commit_models.append(commit_model)

                    # Files per modification
                    for mod in fc.modifications:
                        # Normalize path already done in filtering
                        try:
                            norm_path = graph_normalize(mod.path)
                        except Exception:
                            norm_path = mod.path.replace("\\", "/")
                        old_path_norm = None
                        if mod.old_path:
                            try:
                                old_path_norm = graph_normalize(mod.old_path)
                            except Exception:
                                old_path_norm = mod.old_path.replace("\\", "/")

                        ct = mod.change_type.upper()
                        if ct == "ADD":
                            fid = derive_file_id(repository_id, norm_path)
                            if fid not in file_map:
                                f = File.create(repository_id=repository_id, path=norm_path, alive=True)
                                file_map[fid] = f
                                current_files[norm_path] = f
                            else:
                                # Update alive true
                                existing = file_map[fid]
                                if not existing.alive:
                                    file_map[fid] = File.create(repository_id=repository_id, path=norm_path, alive=True, file_id=fid)
                                    current_files[norm_path] = file_map[fid]
                        elif ct == "DELETE":
                            fid = derive_file_id(repository_id, norm_path)
                            # Retain File node, set alive false
                            # If file not yet in map, try to load from graph or create dead entry
                            if fid in file_map:
                                existing = file_map[fid]
                                # Mark dead
                                file_map[fid] = File(
                                    id=existing.id,
                                    repository_id=existing.repository_id,
                                    path=existing.path,
                                    current_path=existing.current_path,
                                    past_paths=list(existing.past_paths),
                                    alive=False,
                                    is_test=existing.is_test,
                                    language=existing.language,
                                )
                            else:
                                # Create dead node for tracking deletion
                                f = File.create(repository_id=repository_id, path=norm_path, alive=False)
                                file_map[fid] = f
                            # Keep in current_files but marked dead
                            current_files[norm_path] = file_map[fid]
                        elif ct == "MODIFY":
                            fid = derive_file_id(repository_id, norm_path)
                            if fid not in file_map:
                                f = File.create(repository_id=repository_id, path=norm_path, alive=True)
                                file_map[fid] = f
                                current_files[norm_path] = f
                            else:
                                # Ensure alive true
                                existing = file_map[fid]
                                if not existing.alive:
                                    file_map[fid] = File.create(repository_id=repository_id, path=norm_path, alive=True, file_id=fid)
                                    current_files[norm_path] = file_map[fid]
                        elif ct == "RENAME":
                            # High similarity: treat as confident rename
                            # Update File.currentPath, preserve old/new in edge
                            old_norm = old_path_norm or norm_path
                            old_fid = derive_file_id(repository_id, old_norm)
                            new_fid = derive_file_id(repository_id, norm_path)
                            # Find existing file for old path
                            old_file = file_map.get(old_fid) or current_files.get(old_norm)
                            if old_file is not None:
                                # Confident rename: update identity to new path but keep same id? Per spec, preserve identity
                                # We'll use with_rename to keep same id but update path
                                try:
                                    updated = old_file.with_rename(norm_path, confident=True)
                                    # Need to handle id change: with_rename keeps old id, but new path's stable id would be new_fid
                                    # For graph, we need to keep one node; we update the existing file's path
                                    # Remove old mapping, add new mapping but keep updated object with old id
                                    file_map.pop(old_fid, None)
                                    current_files.pop(old_norm, None)
                                    # Store updated under new_fid key? But updated.id is old id, not new_fid
                                    # For upsert, we use file.id as key, so we should keep updated under its id
                                    # Use new_fid as key for future lookups but file.id is old
                                    file_map[new_fid] = updated
                                    current_files[norm_path] = updated
                                    current_files[old_norm] = updated  # keep alias
                                except Exception:
                                    # Fallback: create new node
                                    f = File.create(repository_id=repository_id, path=norm_path, alive=True)
                                    file_map[new_fid] = f
                                    current_files[norm_path] = f
                            else:
                                # No prior file for old path: treat as ADD of new path
                                # Also handle old as dead? But spec says uncertain rename creates distinct node and marks old as not alive
                                # For confident rename with no prior, just create new
                                f = File.create(repository_id=repository_id, path=norm_path, alive=True)
                                # Attempt to find old file in graph via gateway?
                                try:
                                    existing_old = self.gateway.get_file(old_fid, repository_id) if self.gateway else None
                                    if existing_old is not None:
                                        updated = existing_old.with_rename(norm_path, confident=True)
                                        file_map[new_fid] = updated
                                        current_files[norm_path] = updated
                                    else:
                                        file_map[new_fid] = f
                                        current_files[norm_path] = f
                                except Exception:
                                    file_map[new_fid] = f
                                    current_files[norm_path] = f
                            # Mark old path's File as not alive if we created distinct? For confident we preserve single node
                            # So nothing more
                        elif ct == "COPY":
                            # Copy creates distinct File node without merging
                            fid = derive_file_id(repository_id, norm_path)
                            if fid not in file_map:
                                f = File.create(repository_id=repository_id, path=norm_path, alive=True)
                                file_map[fid] = f
                                current_files[norm_path] = f
                            # old path remains as is
                        else:  # UNKNOWN
                            fid = derive_file_id(repository_id, norm_path)
                            if fid not in file_map:
                                f = File.create(repository_id=repository_id, path=norm_path, alive=True)
                                file_map[fid] = f
                                current_files[norm_path] = f

                # Batch writes
                # Authors
                authors_list = list(author_map.values())
                if authors_list:
                    # Chunk authors by batch_size
                    for i in range(0, len(authors_list), self.batch_size):
                        author_chunk = authors_list[i : i + self.batch_size]
                        logger.info("Writing batch %d/%d (authors)", (i // self.batch_size) + 1, (len(authors_list) + self.batch_size - 1) // self.batch_size)
                        self.gateway.upsert_authors(repository_id, author_chunk)
                        total_batches += 1

                # Commits
                if commit_models:
                    for i in range(0, len(commit_models), self.batch_size):
                        commit_chunk = commit_models[i : i + self.batch_size]
                        logger.info("Writing batch %d/%d (commits)", (i // self.batch_size) + 1, (len(commit_models) + self.batch_size - 1) // self.batch_size)
                        self.gateway.upsert_commits(repository_id, commit_chunk)
                        total_batches += 1
                    # Handle fixReason extra property via direct Cypher if needed
                    # For commits labeled as fix, store fixReason on node
                    fix_commits = [(fc.hash, lr.fix_reason) for fc, lr in filtered_commits_labels if lr.is_fix and lr.fix_reason]
                    if fix_commits:
                        # Build batch for fixing fixReason
                        batch_rows = []
                        for h, reason in fix_commits:
                            cid = derive_commit_id(repository_id, h)
                            batch_rows.append({"id": cid, "repository_id": repository_id, "fixReason": reason, "isFix": True})
                        # Use execute_batch with custom Cypher to set fixReason
                        try:
                            driver = getattr(self.gateway, "driver", None) or getattr(self.gateway, "_driver", None)
                            if driver is not None:
                                cypher = """
                                UNWIND $batch AS row
                                MATCH (c:Commit {id: row.id, repository_id: row.repository_id})
                                SET c.isFix = true, c.fixReason = row.fixReason
                                """
                                self.gateway.relationships.execute_batch(cypher, batch_rows, repository_id)
                            else:
                                # Fallback via gateway
                                pass
                        except Exception as e:
                            logger.debug("failed to set fixReason: %s", _redact(str(e)))

                # Files
                files_list = list(file_map.values())
                if files_list:
                    for i in range(0, len(files_list), self.batch_size):
                        file_chunk = files_list[i : i + self.batch_size]
                        logger.info("Writing batch %d/%d (files)", (i // self.batch_size) + 1, (len(files_list) + self.batch_size - 1) // self.batch_size)
                        self.gateway.upsert_files(repository_id, file_chunk)
                        total_batches += 1

                # Relationships: AUTHORED, MODIFIED, CONTAINS, BELONGS_TO etc.
                # AUTHORED
                authored_rows: list[tuple[str, str]] = []
                for fc, _ in filtered_commits_labels:
                    # Re-derive author id
                    try:
                        aid = derive_author_id(repository_id, fc.author_email, fc.author_name)
                    except ValueError:
                        aid = f"{repository_id}:author:{hashlib.sha256(fc.hash.encode()).hexdigest()[:12]}"
                    cid = derive_commit_id(repository_id, fc.hash)
                    authored_rows.append((aid, cid))
                if authored_rows:
                    # Batch authored via gateway
                    for i in range(0, len(authored_rows), self.batch_size):
                        authored_chunk = authored_rows[i : i + self.batch_size]
                        logger.info("Writing batch %d/%d (authored)", (i // self.batch_size) + 1, (len(authored_rows) + self.batch_size - 1) // self.batch_size)
                        self.gateway.relationships.batch_authored(repository_id, authored_chunk)
                        total_batches += 1

                # MODIFIED edges with changeType, oldPath, newPath, additions, deletions
                modified_rows: list[dict[str, Any]] = []
                for fc, _ in filtered_commits_labels:
                    cid = derive_commit_id(repository_id, fc.hash)
                    for mod in fc.modifications:
                        try:
                            norm_path = graph_normalize(mod.path)
                        except Exception:
                            norm_path = mod.path.replace("\\", "/")
                        fid = derive_file_id(repository_id, norm_path)
                        # old/new path evidence
                        old_p = None
                        new_p = norm_path
                        if mod.old_path:
                            try:
                                old_p = graph_normalize(mod.old_path)
                            except Exception:
                                old_p = mod.old_path.replace("\\", "/")
                            # For RENAME/COPY, old path is source, new is dest
                            if mod.change_type.upper() in {"RENAME", "COPY"}:
                                new_p = norm_path
                            else:
                                old_p = old_p
                        modified_rows.append(
                            {
                                "commit_id": cid,
                                "file_id": fid,
                                "additions": mod.added_lines,
                                "deletions": mod.deleted_lines,
                                "change_type": mod.change_type,
                                "oldPath": old_p,
                                "newPath": new_p,
                            }
                        )
                if modified_rows:
                    for i in range(0, len(modified_rows), self.batch_size):
                        mod_chunk = modified_rows[i : i + self.batch_size]
                        logger.info("Writing batch %d/%d (modified)", (i // self.batch_size) + 1, (len(modified_rows) + self.batch_size - 1) // self.batch_size)
                        # Use batch_modified but ensure extra props are persisted
                        # batch_modified will set additions, deletions, change_type, plus oldPath/newPath via props
                        extended = []
                        for row in mod_chunk:
                            extended.append(
                                {
                                    "commit_id": row["commit_id"],
                                    "file_id": row["file_id"],
                                    "additions": row["additions"],
                                    "deletions": row["deletions"],
                                    "change_type": row["change_type"],
                                    "oldPath": row["oldPath"],
                                    "newPath": row["newPath"],
                                }
                            )
                        # Build batch for relationships writer that includes extra props
                        # We use generic execute_batch to ensure oldPath/newPath are set
                        # But batch_modified only knows additions/deletions/change_type
                        # So we call execute_batch with custom cypher that sets all
                        batch_for_cypher = []
                        for r in extended:
                            batch_for_cypher.append(
                                {
                                    "repository_id": repository_id,
                                    "commit_id": r["commit_id"],
                                    "file_id": r["file_id"],
                                    "props": {
                                        "additions": r["additions"],
                                        "deletions": r["deletions"],
                                        "change_type": r["change_type"],
                                        "oldPath": r["oldPath"],
                                        "newPath": r["newPath"],
                                    },
                                }
                            )
                        try:
                            cypher_mod = """
                            UNWIND $batch AS row
                            MATCH (c:Commit {id: row.commit_id, repository_id: row.repository_id})
                            MATCH (f:File {id: row.file_id, repository_id: row.repository_id})
                            MERGE (c)-[rel:MODIFIED {repository_id: row.repository_id}]->(f)
                            ON CREATE SET rel += row.props, rel.created_at = datetime()
                            ON MATCH SET rel += row.props, rel.updated_at = datetime()
                            """
                            self.gateway.relationships.execute_batch(cypher_mod, batch_for_cypher, repository_id)
                        except Exception as e:
                            logger.debug("custom modified batch failed, falling back: %s", _redact(str(e)))
                            # Fallback to standard batch_modified (loses oldPath/newPath but at least writes)
                            self.gateway.relationships.batch_modified(repository_id, mod_chunk)  # type: ignore[arg-type]
                        total_batches += 1

                # CONTAINS and BELONGS_TO
                # CONTAINS: Repository -[:CONTAINS]-> File
                file_ids = [f.id for f in files_list]
                if file_ids:
                    for i in range(0, len(file_ids), self.batch_size):
                        contains_chunk = file_ids[i : i + self.batch_size]
                        self.gateway.relationships.batch_contains(repository_id, contains_chunk)
                        total_batches += 1
                commit_ids = [derive_commit_id(repository_id, fc.hash) for fc, _ in filtered_commits_labels]
                if commit_ids:
                    for i in range(0, len(commit_ids), self.batch_size):
                        belongs_chunk = commit_ids[i : i + self.batch_size]
                        self.gateway.relationships.batch_belongs_to(repository_id, belongs_chunk)
                        total_batches += 1

                # REVERTS relationships where target hash resolvable
                reverts: list[tuple[str, str]] = []
                # Build set of ingested hashes for quick lookup
                ingested_hashes = {fc.hash.lower() for fc, _ in filtered_commits_labels}
                # Also query graph for existing commits in repo for cross-batch reverts
                existing_hashes: set[str] = set()
                if self.gateway is not None:
                    try:
                        # List existing commit hashes via driver query
                        driver = getattr(self.gateway, "driver", None) or getattr(self.gateway, "_driver", None)
                        if driver is not None:
                            with driver.session(database=driver.database) as session:
                                result = session.run(
                                    "MATCH (c:Commit {repository_id: $rid}) RETURN c.hash as hash",
                                    rid=repository_id,
                                )
                                for rec in result.data():
                                    h = rec.get("hash")
                                    if h:
                                        existing_hashes.add(str(h).lower())
                    except Exception:
                        pass
                all_known = ingested_hashes | existing_hashes
                for fc, lr in filtered_commits_labels:
                    if lr.is_revert and lr.revert_target_hash:
                        target = lr.revert_target_hash.lower()
                        if target in all_known:
                            src_id = derive_commit_id(repository_id, fc.hash)
                            # Find target commit id: need full hash; we have prefix maybe
                            # Try to find matching full hash in known sets
                            matched_full = None
                            for known in all_known:
                                if known.startswith(target) or target.startswith(known):
                                    matched_full = known
                                    break
                            if matched_full is None:
                                matched_full = target
                            # Derive target id using matched full hash
                            try:
                                tgt_id = derive_commit_id(repository_id, matched_full)
                            except Exception:
                                tgt_id = f"{repository_id}:{matched_full}"
                            reverts.append((src_id, tgt_id))
                        else:
                            logger.debug("revert target %s not resolvable, skipping REVERTS", target[:8])
                if reverts:
                    for i in range(0, len(reverts), self.batch_size):
                        reverts_chunk = reverts[i : i + self.batch_size]
                        self.gateway.relationships.batch_reverts(repository_id, reverts_chunk)
                        total_batches += 1

                # CO_CHANGED_WITH edges: for each commit that co-modifies >=2 files
                co_changed_counts: dict[tuple[str, str], int] = {}
                for fc, _ in filtered_commits_labels:
                    if len(fc.modifications) < 2:
                        continue
                    fids = []
                    for mod in fc.modifications:
                        try:
                            norm = graph_normalize(mod.path)
                        except Exception:
                            norm = mod.path.replace("\\", "/")
                        fid = derive_file_id(repository_id, norm)
                        fids.append(fid)
                    # Unordered pairs
                    for i in range(len(fids)):
                        for j in range(i + 1, len(fids)):
                            a, b = sorted([fids[i], fids[j]])
                            key = (a, b)
                            co_changed_counts[key] = co_changed_counts.get(key, 0) + 1
                if co_changed_counts:
                    # Prepare batch rows with count, weight
                    rows = []
                    # For weight: count normalized? Use 1.0 * count or similar
                    for (a, b), cnt in co_changed_counts.items():
                        rows.append(
                            {
                                "source_id": a,
                                "target_id": b,
                                "count": cnt,
                                "weight": float(cnt),
                            }
                        )
                    for i in range(0, len(rows), self.batch_size):
                        co_chunk = rows[i : i + self.batch_size]
                        # Use custom Cypher that increments count to handle incremental rebuild without duplicates
                        try:
                            cypher_cc = """
                            UNWIND $batch AS row
                            MATCH (f1:File {id: row.source_id, repository_id: $repository_id})
                            MATCH (f2:File {id: row.target_id, repository_id: $repository_id})
                            MERGE (f1)-[rel:CO_CHANGED_WITH {repository_id: row.repository_id}]->(f2)
                            ON CREATE SET rel.count = row.count, rel.weight = row.weight, rel.created_at = datetime()
                            ON MATCH SET rel.count = rel.count + row.count, rel.weight = rel.weight + row.weight, rel.updated_at = datetime()
                            """
                            # Need to pass repository_id alongside batch; use execute_batch with param per row
                            batch_with_repo = []
                            for r in co_chunk:
                                batch_with_repo.append(
                                    {
                                        "repository_id": repository_id,
                                        "source_id": r["source_id"],
                                        "target_id": r["target_id"],
                                        "count": r["count"],
                                        "weight": r["weight"],
                                    }
                                )
                            # This cypher expects $batch and $repository_id — adapt to execute_batch style
                            # Use a variant where each row carries repository_id
                            cypher_cc2 = """
                            UNWIND $batch AS row
                            MATCH (f1:File {id: row.source_id, repository_id: row.repository_id})
                            MATCH (f2:File {id: row.target_id, repository_id: row.repository_id})
                            MERGE (f1)-[rel:CO_CHANGED_WITH {repository_id: row.repository_id}]->(f2)
                            ON CREATE SET rel.count = row.count, rel.weight = row.weight, rel.created_at = datetime()
                            ON MATCH SET rel.count = rel.count + row.count, rel.weight = rel.weight + row.weight, rel.updated_at = datetime()
                            """
                            self.gateway.relationships.execute_batch(cypher_cc2, batch_with_repo, repository_id)
                        except Exception as e:
                            logger.debug("co-change batch failed, fallback: %s", _redact(str(e)))
                            # Fallback to standard batch_co_changed (sets not increments)
                            self.gateway.relationships.batch_co_changed(repository_id, co_chunk)
                        total_batches += 1
                        logger.info("Writing batch %d/%d (co_changed)", (i // self.batch_size) + 1, (len(rows) + self.batch_size - 1) // self.batch_size)

            state.counters["batches"] = total_batches
            # Update lastIngestedHead only after WRITING_GRAPH succeeds
            if current_head:
                state.last_ingested_head = current_head
                state.last_successful_at = _now_iso()
                if self.gateway is not None:
                    _persist_head(self.gateway, repository_id, current_head, default_branch, canonical)

            # --- COMPLETED ---
            _transition(state, IngestionStatus.COMPLETED)
            state.error_summary = None

            result = IngestionResult(
                state=state,
                commits_processed=state.counters.get("commits", 0),
                files_processed=state.counters.get("files", 0),
                filtered_out=state.counters.get("filteredOut", 0),
                batches=total_batches,
            )
            logger.info(
                "Ingestion completed for %s: %d commits, %d files, %d filtered, %d batches",
                _redact(repository_id),
                result.commits_processed,
                result.files_processed,
                result.filtered_out,
                result.batches,
            )
            return result

        except RewrittenHistoryError as e:
            _transition(state, IngestionStatus.FAILED)
            state.error_summary = _redact(str(e))
            state.needs_full_reingestion = True
            logger.error("Rewritten history for %s: %s", _redact(repository_id), _redact(str(e)))
            raise
        except IngestionError as e:
            _transition(state, IngestionStatus.FAILED)
            state.error_summary = _redact(str(e))
            # Do not update lastIngestedHead on failure
            logger.error("Ingestion failed at %s for %s: %s", e.stage or state.status.value, _redact(repository_id), _redact(str(e)))
            raise
        except Exception as e:
            _transition(state, IngestionStatus.FAILED)
            state.error_summary = _redact(str(e))
            logger.error("Ingestion failed for %s: %s", _redact(repository_id), _redact(str(e)))
            # Wrap as IngestionError preserving stage
            stage = state.status.value if state.status != IngestionStatus.FAILED else "WRITING_GRAPH"
            raise IngestionError(f"ingestion failed at {stage}: {_redact(str(e))}", stage=stage) from e

    def retry(self, repository_id: str) -> IngestionResult:
        """Retry a failed ingestion from PENDING.

        Resets status to PENDING and re-runs ingest with stored canonical URL.
        """
        state = _STATUS_STORE.get(repository_id)
        if state is None:
            # Try to derive from graph
            raise IngestionError(f"no ingestion state found for {repository_id}", stage="PENDING")
        if state.status != IngestionStatus.FAILED:
            raise IngestionError(f"can only retry FAILED ingestion, current is {state.status.value}", stage="PENDING")
        # Reset to PENDING
        state.status = IngestionStatus.PENDING
        state.error_summary = None
        state.needs_full_reingestion = False
        state.stage_timestamps[IngestionStatus.PENDING.value] = _now_iso()
        # Re-run ingest using canonical_url
        return self.ingest(state.canonical_url)

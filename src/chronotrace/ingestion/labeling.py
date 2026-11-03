"""Fix and revert labeling heuristics."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Revert patterns: e.g., Revert "Add feature X" and This reverts commit <hash>
_REVERT_TITLE_RE = re.compile(r'^\s*Revert\s+"[^"]+"', re.IGNORECASE | re.MULTILINE)
_REVERT_HASH_RE = re.compile(r"This\s+reverts\s+commit\s+([0-9a-f]{7,40})", re.IGNORECASE)


@dataclass(slots=True)
class LabelingResult:
    is_fix: bool
    fix_reason: str | None
    is_revert: bool
    revert_target_hash: str | None


def label_commit(
    message: str | None,
    fix_patterns: list[str] | None = None,
    case_sensitive: bool = False,
) -> LabelingResult:
    """Label a commit as fix and/or revert.

    - Fix labeling via configurable case-insensitive regex patterns.
      Stores ``isFix`` and ``fixReason`` (matched pattern + excerpt).
    - Revert detection via ``Revert "..."`` and ``This reverts commit <hash>``.
    - Mixed fix+revert commits get both flags.

    Args:
        message: Commit message (None treated as empty).
        fix_patterns: List of regex patterns (e.g., ``fix``, ``hotfix``).
        case_sensitive: Whether patterns are case-sensitive (default False).
    """
    msg = message or ""
    fix_patterns = fix_patterns or []

    # Fix labeling
    is_fix = False
    fix_reason: str | None = None
    flags = 0 if case_sensitive else re.IGNORECASE
    for pat in fix_patterns:
        if not pat:
            continue
        try:
            regex = re.compile(pat, flags)
        except re.error:
            # Invalid regex should have been rejected at config validation;
            # skip here defensively
            continue
        m = regex.search(msg)
        if m:
            is_fix = True
            # Build fixReason: matched pattern + excerpt (first 80 chars or matched snippet)
            matched = m.group(0)
            excerpt = msg.strip().replace("\n", " ")[:120]
            # Truncate excerpt for readability but preserve enough context
            if len(excerpt) > 120:
                excerpt = excerpt[:120] + "..."
            fix_reason = f"pattern={pat!r} matched={matched!r} excerpt={excerpt!r}"
            break

    # Revert detection
    is_revert = False
    revert_hash: str | None = None

    if _REVERT_TITLE_RE.search(msg):
        is_revert = True
    m_hash = _REVERT_HASH_RE.search(msg)
    if m_hash:
        is_revert = True
        revert_hash = m_hash.group(1).lower()

    return LabelingResult(
        is_fix=is_fix,
        fix_reason=fix_reason,
        is_revert=is_revert,
        revert_target_hash=revert_hash,
    )


def detect_revert_target(message: str | None) -> str | None:
    """Extract reverted commit hash from message if present."""
    if not message:
        return None
    m = _REVERT_HASH_RE.search(message)
    if m:
        return m.group(1).lower()
    return None


def is_revert_commit(message: str | None) -> bool:
    """Return True if message matches revert pattern."""
    if not message:
        return False
    return bool(_REVERT_TITLE_RE.search(message) or _REVERT_HASH_RE.search(message))

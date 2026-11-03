"""Ingestion package — acquisition, extraction, filtering, labeling, orchestration."""

from .acquisition import (
    AcquisitionError,
    canonicalize_url,
    derive_local_path,
    derive_repository_id,
    validate_url,
)
from .extractor import CommitData, ModificationData, extract_commits
from .filtering import FilteringResult, filter_commits, normalize_path
from .labeling import LabelingResult, detect_revert_target, label_commit
from .service import IngestionService, IngestionStatus

__all__ = [
    "AcquisitionError",
    "CommitData",
    "FilteringResult",
    "IngestionService",
    "IngestionStatus",
    "LabelingResult",
    "ModificationData",
    "canonicalize_url",
    "derive_local_path",
    "derive_repository_id",
    "detect_revert_target",
    "extract_commits",
    "filter_commits",
    "label_commit",
    "normalize_path",
    "validate_url",
]

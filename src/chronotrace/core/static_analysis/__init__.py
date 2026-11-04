"""Static analysis package — scanner, parser, mapper, resolver, service."""

from .models import AnalysisResult, Diagnostics, ParseFailure, RawImport, UnresolvedImport
from .scanner import canonical_path, discover_python_files, discover_source_files

try:
    from .service import analyze_repository
except ImportError:  # service not yet implemented
    analyze_repository = None  # type: ignore[assignment]

__all__ = [
    "AnalysisResult",
    "Diagnostics",
    "ParseFailure",
    "RawImport",
    "UnresolvedImport",
    "analyze_repository",
    "canonical_path",
    "discover_python_files",
    "discover_source_files",
]

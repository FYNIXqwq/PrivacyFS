"""Explicit, local document inspection; legacy scans remain filename-only."""
from .models import Limits, ParseStatus
from .session import DocumentSession
from .inspection import RuleInspector, inspect_document, public_report

__all__ = ["DocumentSession", "Limits", "ParseStatus", "RuleInspector", "inspect_document", "public_report"]

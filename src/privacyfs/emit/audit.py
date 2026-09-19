"""Safe renderers for release-level context risk reports."""

from __future__ import annotations

import json

from ..models import AnalysisResult, RiskFinding


def _risk_payload(risk: RiskFinding) -> dict:
    return {
        "risk_id": risk.risk_id,
        "type": risk.risk_type,
        "severity": risk.severity,
        "score": risk.score,
        "subjects": list(risk.subject_ids),
        "entries": list(risk.entry_ids),
        "signal_categories": list(risk.signal_categories),
        "support": risk.support,
        "explanation": risk.explanation,
        "recommended_actions": list(risk.recommended_actions),
    }


def analysis_to_json(result: AnalysisResult) -> str:
    payload = {
        "summary": {
            "entries": result.entries,
            "subjects": result.subjects,
            "risks": len(result.risks),
            "high_risks": sum(risk.severity == "high" for risk in result.risks),
            "k_anonymity": result.k_anonymity,
        },
        "risks": [_risk_payload(risk) for risk in result.risks],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_analysis(result: AnalysisResult) -> str:
    lines = [
        "PrivacyFS context audit",
        (
            f"{result.entries} entries, {result.subjects} subjects, "
            f"{len(result.risks)} risks, k={result.k_anonymity}"
        ),
    ]
    if not result.risks:
        lines.append("No combination risks matched the current policy.")
        return "\n".join(lines)

    for risk in result.risks:
        support = "n/a" if risk.support is None else str(risk.support)
        lines.extend([
            "",
            f"[{risk.severity.upper()}] {risk.risk_type} ({risk.risk_id})",
            f"  subjects: {', '.join(risk.subject_ids)}",
            f"  entries: {', '.join(risk.entry_ids)}",
            f"  signals: {', '.join(risk.signal_categories) or 'ENTITY_LINK'}",
            f"  support: {support}",
            f"  reason: {risk.explanation}",
            f"  actions: {', '.join(risk.recommended_actions)}",
        ])
    return "\n".join(lines)

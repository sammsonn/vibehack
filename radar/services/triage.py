"""Risk triage: deterministic path rules first, the LLM can only raise risk, never lower it."""
from radar.config import Settings
from radar.core import rules
from radar.core.models import PR, Triage
from radar.integrations import llm


def triage(s: Settings, pr: PR) -> Triage:
    rule_reasons = rules.high_risk_reasons(pr.files, pr.additions + pr.deletions)
    data = llm.ask(s, pr)
    if data is None:
        return Triage(
            summary=pr.title,
            risk="high" if rule_reasons else "low",
            risk_reason=f"Touches {', '.join(rule_reasons)}" if rule_reasons else "No high-risk paths (AI summary unavailable)",
            look_first=pr.files[0] if pr.files else "",
        )
    if rule_reasons:
        reason = f"Touches {', '.join(rule_reasons)}"
        if data["risk"] == "high":
            reason += f". {data['risk_reason']}"
        return Triage(data["summary"].strip(), "high", reason, data["look_first"].strip())
    return Triage(data["summary"].strip(), data["risk"], data["risk_reason"].strip(), data["look_first"].strip())

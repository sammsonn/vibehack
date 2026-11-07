"""Risk triage: deterministic path rules first, the LLM can only raise risk, never lower it."""
import logging

from radar.config import Settings
from radar.core import rules
from radar.core.models import PR, Triage
from radar.integrations import llm
from radar.storage import store

log = logging.getLogger(__name__)


def triage(s: Settings, pr: PR) -> Triage:
    rule_reasons = rules.high_risk_reasons(pr.files, pr.additions + pr.deletions)
    data, usage = llm.ask(s, pr)
    if usage.total:
        try:
            store.record_llm_usage(s.llm_model, pr.number, usage)
        except Exception:  # cost tracking is best-effort
            log.warning("could not record LLM usage for PR #%s", pr.number, exc_info=True)
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

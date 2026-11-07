import json
import logging

from openai import OpenAI

from radar import rules
from radar.config import Settings
from radar.models import PR, Triage

log = logging.getLogger(__name__)

SYSTEM = (
    "You triage GitHub pull requests for a busy engineering lead. Reply with ONLY a JSON object: "
    '{"summary": str (max 2 sentences, what changes and why), "risk": "low"|"high", '
    '"risk_reason": str (one sentence), "look_first": str (the file or area a reviewer should open first)}. '
    "High risk = auth, payments, database migrations, infra, security, possible data loss. Otherwise low."
)
MAX_PATCH_CHARS = 3000
MAX_DIFF_CHARS = 12000


def build_diff(files: list) -> str:
    """files: PyGithub File objects."""
    parts = [f"--- {f.filename} (+{f.additions}/-{f.deletions})\n{(f.patch or '')[:MAX_PATCH_CHARS]}" for f in files]
    return "\n".join(parts)[:MAX_DIFF_CHARS]


def _parse(raw: str) -> dict | None:
    try:
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("risk") not in ("low", "high"):
        return None
    if not all(isinstance(data.get(k), str) and data[k].strip() for k in ("summary", "risk_reason", "look_first")):
        return None
    return data


def _ask(s: Settings, pr: PR) -> dict | None:
    client = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=60)
    prompt = f"PR #{pr.number}: {pr.title}\n{pr.body[:1500]}\n\n{pr.diff}"
    for attempt in range(2):
        try:
            reply = client.chat.completions.create(
                model=s.llm_model,
                temperature=0,
                max_tokens=1500,  # reasoning models spend part of this before answering
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            )
            data = _parse(reply.choices[0].message.content or "")
            if data:
                return data
            log.warning("PR #%s: unparseable LLM reply (attempt %d)", pr.number, attempt + 1)
        except Exception as e:  # network, rate limit, model errors: fall back to rules
            log.warning("PR #%s: LLM call failed (attempt %d): %s", pr.number, attempt + 1, e)
    return None


def triage(s: Settings, pr: PR) -> Triage:
    rule_reasons = rules.high_risk_reasons(pr.files, pr.additions + pr.deletions)
    data = _ask(s, pr)
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

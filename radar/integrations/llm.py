"""LLM client: asks the model for a PR summary and risk, returns validated JSON or None."""
import json
import logging

from openai import OpenAI

from radar.config import Settings
from radar.core.models import PR, TokenUsage

log = logging.getLogger(__name__)

SYSTEM = (
    "You triage GitHub pull requests for a busy engineering lead. Reply with ONLY a JSON object: "
    '{"summary": str (max 2 sentences, what changes and why), "risk": "low"|"high", '
    '"risk_reason": str (one sentence), "look_first": str (the file or area a reviewer should open first)}. '
    "High risk = auth, payments, database migrations, infra, security, possible data loss. Otherwise low."
)


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


def _usage(reply) -> TokenUsage:
    u = getattr(reply, "usage", None)
    return TokenUsage(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)


def ask(s: Settings, pr: PR) -> tuple[dict | None, TokenUsage]:
    """Validated JSON (or None) plus the tokens spent on every attempt, including failed ones."""
    client = OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key, timeout=60)
    usage = TokenUsage()
    prompt = f"PR #{pr.number}: {pr.title}\n{pr.body[:1500]}\n\n{pr.diff}"
    for attempt in range(2):
        try:
            reply = client.chat.completions.create(
                model=s.llm_model,
                temperature=0,
                max_tokens=1500,  # reasoning models spend part of this before answering
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            )
            usage += _usage(reply)
            data = _parse(reply.choices[0].message.content or "")
            if data:
                return data, usage
            log.warning("PR #%s: unparseable LLM reply (attempt %d)", pr.number, attempt + 1)
        except Exception as e:  # network, rate limit, model errors: fall back to rules
            log.warning("PR #%s: LLM call failed (attempt %d): %s", pr.number, attempt + 1, e)
    return None, usage



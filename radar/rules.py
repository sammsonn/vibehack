"""Deterministic risk rules. They run before the LLM, and the LLM can only raise risk, never lower it."""
import re

HIGH_RISK_PATHS = [
    (re.compile(r"(^|/)(auth|oauth|login|sessions?|sso|permissions?|acl)(/|\.|_|$)", re.I), "auth"),
    (re.compile(r"(^|/)(payments?|billing|stripe|checkout|invoices?)(/|\.|_|$)", re.I), "payments"),
    (re.compile(r"(^|/)migrations?/|\.sql$|(^|/)schema\.", re.I), "database migration"),
    (re.compile(r"\.tf$|(^|/)(infra|terraform|k8s|helm|deploy)/|(^|/)Dockerfile$|docker-compose", re.I), "infra"),
    (re.compile(r"^\.github/workflows/", re.I), "CI pipeline"),
    (re.compile(r"(^|/)(security|crypto|secrets?)(/|\.|_|$)|\.pem$", re.I), "security"),
]
LARGE_DIFF_LINES = 500


def high_risk_reasons(files: tuple[str, ...] | list[str], changed_lines: int = 0) -> list[str]:
    reasons: list[str] = []
    for pattern, label in HIGH_RISK_PATHS:
        if label not in reasons and any(pattern.search(f) for f in files):
            reasons.append(label)
    if changed_lines > LARGE_DIFF_LINES:
        reasons.append(f"large diff ({changed_lines} lines)")
    return reasons

import datetime as dt
import unittest
from collections import Counter
from unittest import mock
from zoneinfo import ZoneInfo

from radar import calendar_api, llm, ranking, rules, slack_ui, stats
from radar.config import Member, Settings, Team
from radar.models import PR, Candidate, Suggestion, Triage

TZ = ZoneInfo("Europe/Bucharest")
NOW = dt.datetime(2026, 11, 7, 16, 50, tzinfo=TZ)
LEAD = Member("Samson", "lead", "lead@x.com")
ANDREI = Member("Andrei", "andrei", "andrei@x.com")
CONE = Member("Cone", "cone", "cone@x.com")
TEAM = Team(lead=LEAD, members=(LEAD, ANDREI, CONE))


def at(hour: int, minute: int = 0) -> dt.datetime:
    return NOW.replace(hour=hour, minute=minute)


def make_pr(number=1, author="andrei", files=("README.md",), changed=2) -> PR:
    return PR(number, f"PR {number}", f"https://github.com/o/r/pull/{number}", author, "abc",
              at(10), files, changed, 0, "", "")


LOW = Triage("Fixes a typo.", "low", "Docs only", "README.md")
HIGH = Triage("Adds retries.", "high", "Touches payments", "payments/webhook.py")


class RulesTest(unittest.TestCase):
    def test_flags_risky_paths(self):
        self.assertEqual(rules.high_risk_reasons(["payments/webhook.py", "migrations/0042.sql"]),
                         ["payments", "database migration"])
        self.assertEqual(rules.high_risk_reasons([".github/workflows/ci.yml"]), ["CI pipeline"])
        self.assertEqual(rules.high_risk_reasons(["src/auth_service.py"]), ["auth"])

    def test_ignores_safe_paths(self):
        self.assertEqual(rules.high_risk_reasons(["README.md", "tests/test_utils.py", "src/author.py"]), [])

    def test_large_diff(self):
        self.assertEqual(rules.high_risk_reasons(["a.py"], 900), ["large diff (900 lines)"])


class CalendarMathTest(unittest.TestCase):
    calls = [(at(17), at(18)), (at(18), at(19))]

    def test_busy_until_merges_back_to_back_calls(self):
        self.assertEqual(calendar_api.busy_until(self.calls, at(17, 10)), at(19))
        self.assertIsNone(calendar_api.busy_until(self.calls, at(16, 50)))

    def test_first_free_slot_after_calls(self):
        self.assertEqual(calendar_api.first_free_slot(self.calls, at(17), 30, TZ), (at(19), at(19, 30)))

    def test_first_free_slot_fits_before_calls(self):
        self.assertEqual(calendar_api.first_free_slot(self.calls, at(16), 30, TZ), (at(16), at(16, 30)))

    def test_first_free_slot_rolls_to_next_morning(self):
        slot = calendar_api.first_free_slot([(at(19), at(20))], at(19), 30, TZ)
        self.assertEqual(slot[0], dt.datetime(2026, 11, 8, 9, 0, tzinfo=TZ))

    def test_is_free(self):
        self.assertFalse(calendar_api.is_free(self.calls, at(16, 50), at(17, 50)))
        self.assertTrue(calendar_api.is_free(self.calls, at(15), at(16)))


class RankingTest(unittest.TestCase):
    def test_excludes_lead_and_author(self):
        ranked = ranking.rank(make_pr(author="andrei"), TEAM, Counter(), Counter(), {}, NOW)
        self.assertEqual([c.github for c in ranked], ["cone"])

    def test_free_beats_expert_then_expert_beats_load(self):
        team = Team(LEAD, (LEAD, ANDREI, CONE, Member("Ana", "ana", "ana@x.com")))
        pr = make_pr(author="lead2")
        busy = {"andrei@x.com": [(at(16, 30), at(18))]}
        ranked = ranking.rank(pr, team, Counter(andrei=9, cone=2), Counter(ana=0, cone=3), busy, NOW)
        self.assertEqual([c.github for c in ranked], ["cone", "ana", "andrei"])
        self.assertEqual(ranked[2].busy_until, at(18))

    def test_low_risk_with_free_teammate_hands_off(self):
        ranked = ranking.rank(make_pr(), TEAM, Counter(cone=3), Counter(), {}, NOW)
        sg = ranking.decide(make_pr(), LOW, ranked, (at(19), at(19, 30)), at(19))
        self.assertEqual((sg.action, sg.reviewer.github), ("handoff", "cone"))

    def test_high_risk_stays_with_lead(self):
        ranked = ranking.rank(make_pr(), TEAM, Counter(), Counter(), {}, NOW)
        sg = ranking.decide(make_pr(), HIGH, ranked, (at(19), at(19, 30)), at(19))
        self.assertEqual((sg.action, sg.slot), ("block", (at(19), at(19, 30))))

    def test_low_risk_but_everyone_busy_falls_back_to_block(self):
        busy = {"cone@x.com": [(at(16), at(18))]}
        ranked = ranking.rank(make_pr(), TEAM, Counter(), Counter(), busy, NOW)
        sg = ranking.decide(make_pr(), LOW, ranked, (at(19), at(19, 30)), at(19))
        self.assertEqual(sg.action, "block")
        self.assertIn("No free teammate", sg.why)


class SuggestionJsonTest(unittest.TestCase):
    def test_round_trip(self):
        reviewer = Candidate("cone", "Cone", True, 3, 1, None)
        sg = Suggestion(make_pr(), LOW, "handoff", "why", reviewer, (at(19), at(19, 30)), at(19))
        back = Suggestion.from_json(sg.to_json())
        self.assertEqual(back.reviewer, reviewer)
        self.assertEqual(back.slot, sg.slot)
        self.assertEqual(back.pr.files, ("README.md",))


class LlmTest(unittest.TestCase):
    settings = mock.Mock(spec=Settings)

    def test_parse_rejects_bad_json(self):
        self.assertIsNone(llm._parse("not json"))
        self.assertIsNone(llm._parse('{"summary": "x", "risk": "medium", "risk_reason": "r", "look_first": "f"}'))
        self.assertEqual(llm._parse('Sure! {"summary": "x", "risk": "low", "risk_reason": "r", "look_first": "f"}')["risk"], "low")

    def test_rules_override_llm_low(self):
        pr = make_pr(files=("payments/webhook.py",))
        with mock.patch.object(llm, "_ask", return_value={"summary": "s", "risk": "low", "risk_reason": "r", "look_first": "f"}):
            self.assertEqual(llm.triage(self.settings, pr).risk, "high")

    def test_falls_back_to_rules_without_llm(self):
        with mock.patch.object(llm, "_ask", return_value=None):
            t = llm.triage(self.settings, make_pr(files=("migrations/1.sql",)))
        self.assertEqual((t.risk, t.summary), ("high", "PR 1"))


class RenderTest(unittest.TestCase):
    def test_pending_card_has_buttons_and_approve_all(self):
        handoff = Suggestion(make_pr(1), LOW, "handoff", "why", Candidate("cone", "Cone", True, 3, 0), None, at(19))
        block = Suggestion(make_pr(3), HIGH, "block", "High risk", None, (at(19), at(19, 30)), at(19))
        card = {"id": 7, "created_at": NOW.isoformat(), "header": "📅 *Demo* starts in 10 min"}
        rows = [{"id": i, "payload": sg.to_json(), "status": "pending", "decided_by": None, "decided_at": None,
                 "snoozed_until": None, "result": None} for i, sg in enumerate([block, handoff], start=1)]
        _, blocks = slack_ui.render(card, rows, TZ)
        ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        self.assertIn("radar:approve:1", ids)
        self.assertIn("radar:all:7", ids)
        labels = [e["text"]["text"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        self.assertIn("📅 Block 19:00–19:30 to review", labels)
        self.assertIn("✅ Hand off to Cone", labels)


class StatsTest(unittest.TestCase):
    def rec(self, number, start_h, review_h=None, open_=False):
        return stats.ReviewRecord(number, "t", "andrei", at(start_h), at(review_h) if review_h else None,
                                  "cone" if review_h else None, open_)

    def row(self, pr, status, suggested_h, decided_h):
        return {"pr_number": pr, "status": status, "suggested_at": at(suggested_h).isoformat(),
                "decided_at": at(decided_h).isoformat() if decided_h else None}

    def test_splits_prs_by_radar_approval_before_first_review(self):
        records = [
            self.rec(1, 9, 15),           # no Radar: 6h
            self.rec(2, 10, 12),          # handed off at 11, reviewed 12: with Radar, 2h
            self.rec(3, 10, 11),          # approved only after the review happened: without Radar, 1h
            self.rec(4, 12, open_=True),  # kept, still waiting
        ]
        rows = [self.row(2, "handed_off", 10, 11), self.row(3, "blocked", 11, 12), self.row(4, "kept", 13, 14),
                self.row(4, "superseded", 12, None)]
        st = stats.compute(records, rows, at(16))
        self.assertEqual(sorted(st.without_radar), [dt.timedelta(hours=1), dt.timedelta(hours=6)])
        self.assertEqual(st.with_radar, [dt.timedelta(hours=2)])
        self.assertEqual(st.still_waiting, [(4, dt.timedelta(hours=4), False)])
        self.assertEqual(st.radar_prs, {2})
        self.assertEqual(st.decision_times, [dt.timedelta(hours=1)] * 3)
        text = stats.render(st, "o/r", 30)
        self.assertIn("With Radar: *2h 00m* median (n=1)", text)
        self.assertIn("3 suggested · 2 approved (1 handoff, 1 review block) · 1 kept", text)


if __name__ == "__main__":
    unittest.main()

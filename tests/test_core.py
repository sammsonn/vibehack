"""radar.core: pure logic, no I/O."""
import datetime as dt
import unittest
from collections import Counter

from radar.config import Member, Team
from radar.core import metrics, ranking, rules, schedule
from radar.core.models import Candidate, Suggestion
from tests.helpers import HIGH, LOW, NOW, TEAM, TZ, ANDREI, CONE, LEAD, at, make_pr


class TeamTest(unittest.TestCase):
    def test_display_name(self):
        self.assertEqual(TEAM.display_name("ANDREI"), "Andrei")
        self.assertEqual(TEAM.display_name("dependabot[bot]"), "dependabot[bot]")


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
        self.assertEqual(schedule.busy_until(self.calls, at(17, 10)), at(19))
        self.assertIsNone(schedule.busy_until(self.calls, at(16, 50)))

    def test_first_free_slot_after_calls(self):
        self.assertEqual(schedule.first_free_slot(self.calls, at(17), 30, TZ), (at(19), at(19, 30)))

    def test_first_free_slot_fits_before_calls(self):
        self.assertEqual(schedule.first_free_slot(self.calls, at(16), 30, TZ), (at(16), at(16, 30)))

    def test_first_free_slot_rolls_to_next_morning(self):
        slot = schedule.first_free_slot([(at(19), at(20))], at(19), 30, TZ)
        self.assertEqual(slot[0], dt.datetime(2026, 11, 8, 9, 0, tzinfo=TZ))

    def test_is_free(self):
        self.assertFalse(schedule.is_free(self.calls, at(16, 50), at(17, 50)))
        self.assertTrue(schedule.is_free(self.calls, at(15), at(16)))


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


class StatsTest(unittest.TestCase):
    def rec(self, number, start_h, review_h=None, open_=False):
        return metrics.ReviewRecord(number, "t", "andrei", at(start_h), at(review_h) if review_h else None,
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
        st = metrics.compute(records, rows, at(16))
        self.assertEqual(sorted(st.without_radar), [dt.timedelta(hours=1), dt.timedelta(hours=6)])
        self.assertEqual(st.with_radar, [dt.timedelta(hours=2)])
        self.assertEqual(st.still_waiting, [(4, dt.timedelta(hours=4), False)])
        self.assertEqual(st.radar_prs, {2})
        self.assertEqual(st.decision_times, [dt.timedelta(hours=1)] * 3)
        text = metrics.render(st, "o/r", 30)
        self.assertIn("With Radar: *2h 00m* median (n=1)", text)
        self.assertIn("3 suggested · 2 approved (1 handoff, 1 review block) · 1 kept", text)


class DescribeSlotTest(unittest.TestCase):
    def test_today_tomorrow_later(self):
        day = dt.timedelta(days=1)
        self.assertEqual(schedule.describe_slot((at(19), at(19, 30)), TZ, NOW), "19:00–19:30")
        self.assertEqual(schedule.describe_slot((at(9, 30) + day, at(10) + day), TZ, NOW), "tomorrow 09:30–10:00")
        self.assertEqual(schedule.describe_slot((at(9) + 2 * day, at(9, 30) + 2 * day), TZ, NOW), "Mon 09 Nov 09:00–09:30")


class SavingsTest(unittest.TestCase):
    def stats(self, without_h, with_h):
        hours = lambda hs: [dt.timedelta(hours=h) for h in hs]  # noqa: E731
        return metrics.Stats(hours(without_h), hours(with_h), [], Counter(), [], set())

    def savings(self, st):
        return metrics.savings(st, fallback_baseline_hours=8, blocked_cost_factor=0.25, hourly_eur=20,
                               prompt_tokens=200_000, completion_tokens=40_000, usd_per_m_input=0.10, usd_per_m_output=0.50)

    def test_uses_live_baseline_when_enough_data(self):
        sv = self.savings(self.stats([10, 12, 20], [2, 4, 15]))  # baseline 12h; 15h is slower, counts as 0 not negative
        self.assertEqual(sv.baseline, dt.timedelta(hours=12))
        self.assertIn("n=3", sv.baseline_source)
        self.assertEqual(sv.wait_avoided, dt.timedelta(hours=18))
        self.assertAlmostEqual(sv.dev_hours, 4.5)
        self.assertAlmostEqual(sv.eur, 90)
        self.assertAlmostEqual(sv.model_usd, 0.04)

    def test_falls_back_to_interview_baseline(self):
        sv = self.savings(self.stats([10], [2]))
        self.assertEqual((sv.baseline, sv.baseline_source), (dt.timedelta(hours=8), "BASELINE_WAIT_HOURS from the interview"))
        self.assertAlmostEqual(sv.dev_hours, 1.5)

    def test_line(self):
        lines = metrics.savings_lines(self.savings(self.stats([10, 12, 20], [2, 4, 15])), 7)
        self.assertEqual(lines[0], "💰 *Saved ~4.5 developer-hours this week ≈ €90* vs ~$0.04 model cost")
        self.assertIn("240,000 LLM tokens", lines[1])
        self.assertIn("in the last 30 days", metrics.savings_lines(self.savings(self.stats([], [])), 30)[0])


if __name__ == "__main__":
    unittest.main()

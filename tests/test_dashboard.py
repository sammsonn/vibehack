"""App Home dashboard: the pure math in radar.core.dashboard and the view in radar.slack.home."""
import datetime as dt
import unittest
from collections import Counter

from radar.core import dashboard, metrics
from radar.core.dashboard import OpenPR, Snapshot
from radar.core.models import Candidate, Suggestion
from radar.slack import home
from tests.helpers import ANDREI, CONE, LEAD, LOW, NOW, TEAM, TZ, at, make_pr


def pr(number, author="andrei", draft=False, **requested) -> OpenPR:
    return OpenPR(number, f"PR {number}", f"https://github.com/o/r/pull/{number}", author, draft, at(9), requested)


def snapshot(prs, busy=None, handoffs=(), stats=None, savings=None) -> Snapshot:
    return Snapshot(NOW, NOW + dt.timedelta(hours=12), list(prs), busy or {}, list(handoffs), stats, savings)


def handoff_row(number, to, decided_at):
    sg = Suggestion(make_pr(number), LOW, "handoff", "why", Candidate(to, to, True, 0, 0))
    return {"status": "handed_off", "decided_at": decided_at.isoformat(), "payload": sg.to_json(),
            "handed_to": to, "pr_number": number}


class CoreTest(unittest.TestCase):
    prs = [
        pr(1, lead=at(10), cone=at(15)),
        pr(2, author="cone", lead=at(16)),
        pr(3, draft=True, lead=at(8)),  # drafts never wait on anyone
        pr(4, author="lead"),
    ]

    def test_waiting_on_skips_drafts_and_sorts_longest_first(self):
        waits = dashboard.waiting_on(snapshot(self.prs), "LEAD")
        self.assertEqual([(p.number, w) for p, w in waits],
                         [(1, dt.timedelta(hours=6, minutes=50)), (2, dt.timedelta(minutes=50))])

    def test_team_load_counts_reviews_authored_and_calendar(self):
        busy = {CONE.email: [(at(16), at(17, 30))]}
        loads = {ld.github: ld for ld in dashboard.team_load(snapshot(self.prs, busy), TEAM)}
        self.assertEqual((loads["lead"].reviews, loads["lead"].authored, loads["lead"].busy_until), (2, 1, None))
        self.assertEqual((loads["cone"].reviews, loads["cone"].authored, loads["cone"].busy_until), (1, 1, at(17, 30)))
        self.assertEqual(loads["andrei"].authored, 2)
        self.assertEqual(dashboard.team_load(snapshot(self.prs), TEAM)[0].github, "lead")  # busiest first

    def test_stale_lists_old_requests(self):
        self.assertEqual([(p.number, who) for p, who, _ in dashboard.stale(snapshot(self.prs), 4)], [(1, "lead")])

    def test_handoff_state_follows_github(self):
        rows = [handoff_row(1, "cone", at(15)), handoff_row(2, "andrei", at(15)), handoff_row(9, "cone", at(14)),
                handoff_row(5, "cone", NOW - dt.timedelta(days=10))]
        open_prs = [pr(1, cone=at(15)), pr(2, author="cone")]  # #2: Andrei reviewed; #9: closed
        result = dashboard.handoffs(rows, open_prs, NOW - dt.timedelta(days=7))
        self.assertEqual([(h.pr_number, h.state) for h in result], [(1, "waiting"), (2, "reviewed"), (9, "closed")])


class HomeViewTest(unittest.TestCase):
    def texts(self, view):
        out = []
        for b in view["blocks"]:
            if "text" in b:
                out.append(b["text"]["text"])
            out += [f["text"] for f in b.get("fields", [])]
            out += [e["text"] for e in b.get("elements", []) if isinstance(e.get("text"), str)]
        return "\n".join(out)

    def test_bar(self):
        self.assertEqual(home.bar(0, 4, 4), "`░░░░`")
        self.assertEqual(home.bar(2, 4, 4), "`██░░`")
        self.assertEqual(home.bar(9, 4, 4), "`████`")
        self.assertEqual(home.bar(0.1, 10, 4), "`█░░░`")  # anything above zero shows

    def test_developer_sees_own_queue_and_handoffs_but_not_team_sections(self):
        snap = snapshot([pr(1, author="cone", andrei=at(12))],
                        handoffs=[dashboard.Handoff(1, "PR 1", "u", "andrei", at(12), "waiting")])
        view = home.render(snap, ANDREI, TEAM, TZ, 4)
        text = self.texts(view)
        self.assertIn("👀 Waiting on you · 1", text)
        self.assertIn("🔴 *<https://github.com/o/r/pull/1|#1 PR 1>*", text)  # 4h 50m > 4h stale
        self.assertIn("from Samson", text)
        self.assertIn("waiting on your review", text)
        self.assertNotIn("Team load", text)
        self.assertNotIn("The number", text)
        ids = [e["action_id"] for b in view["blocks"] if b["type"] == "actions" for e in b["elements"]]
        self.assertEqual(ids, ["home:refresh"])

    def test_lead_sees_team_load_bottlenecks_and_the_number(self):
        prs = [pr(1, lead=at(9)), pr(2, lead=at(10)), pr(3, author="cone", lead=at(16))]
        st = metrics.Stats([dt.timedelta(hours=8)], [dt.timedelta(hours=2)], [], Counter(handed_off=2, kept=1), [], set())
        sv = metrics.savings(st, fallback_baseline_hours=8, blocked_cost_factor=0.25, hourly_eur=20,
                             prompt_tokens=1000, completion_tokens=500, usd_per_m_input=0.1, usd_per_m_output=0.5)
        snap = snapshot(prs, busy={LEAD.email: [(at(16), at(18))]}, stats=st, savings=sv)
        view = home.render(snap, LEAD, TEAM, TZ, 4)
        text = self.texts(view)
        self.assertIn("👥 Team load", text)
        self.assertIn("🧱 *Samson* holds 3 of 3 open review requests (100%)", text)
        self.assertIn("📅 *Samson* has 3 reviews waiting but is busy until 18:00", text)
        self.assertIn("🔴 *2 review requests* older than 4h", text)
        self.assertIn("4.0× faster", text)
        self.assertIn("💰 The number · this week", text)
        ids = [e["action_id"] for b in view["blocks"] if b["type"] == "actions" for e in b["elements"]]
        self.assertIn("home:sweep", ids)
        self.assertLessEqual(len(view["blocks"]), 100)

    def test_empty_queue_and_missing_stats(self):
        text = self.texts(home.render(snapshot([]), LEAD, TEAM, TZ, 4))
        self.assertIn("Inbox zero", text)
        self.assertIn("No bottlenecks", text)
        self.assertIn("Stats are unavailable", text)


if __name__ == "__main__":
    unittest.main()

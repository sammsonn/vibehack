"""radar.services: triage, reminders, merge gate, sweep context and approved actions (external calls mocked)."""
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from radar.config import Member, Settings, Team
from radar.core import ranking
from radar.core.models import Candidate, Suggestion
from radar.integrations import llm
from radar.services import actions, approval_gate, digest, reminders, sweep, triage
from radar.slack import blocks, messages
from radar.storage import store
from tests.helpers import HIGH, LOW, NOW, TEAM, TZ, ANDREI, CONE, LEAD, at, make_pr


class LlmTest(unittest.TestCase):
    settings = mock.Mock(spec=Settings)

    def test_parse_rejects_bad_json(self):
        self.assertIsNone(llm._parse("not json"))
        self.assertIsNone(llm._parse('{"summary": "x", "risk": "medium", "risk_reason": "r", "look_first": "f"}'))
        self.assertEqual(llm._parse('Sure! {"summary": "x", "risk": "low", "risk_reason": "r", "look_first": "f"}')["risk"], "low")

    def test_rules_override_llm_low(self):
        pr = make_pr(files=("payments/webhook.py",))
        with mock.patch.object(triage.llm, "ask", return_value={"summary": "s", "risk": "low", "risk_reason": "r", "look_first": "f"}):
            self.assertEqual(triage.triage(self.settings, pr).risk, "high")

    def test_falls_back_to_rules_without_llm(self):
        with mock.patch.object(triage.llm, "ask", return_value=None):
            t = triage.triage(self.settings, make_pr(files=("migrations/1.sql",)))
        self.assertEqual((t.risk, t.summary), ("high", "PR 1"))


class ReminderTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()
        self.settings = mock.Mock(spec=Settings)
        self.settings.team = TEAM
        reviewer = Candidate("cone", "Cone", True, 0, 0)
        self.sgs = [Suggestion(make_pr(1), LOW, "handoff", "why", reviewer, None, at(19)),
                    Suggestion(make_pr(2), LOW, "handoff", "why", reviewer, None, at(19))]
        with mock.patch.object(store, "_now", return_value=at(16, 50).isoformat()):
            self.card = store.create_card("precall", "h", self.sgs)
        store.set_card_message(self.card, "D1", "123.4")

    def due(self, now, waiting=(1, 2)):
        with mock.patch.object(reminders.github, "live_repo"), \
             mock.patch.object(reminders.github, "still_waiting_on", side_effect=lambda r, n, l: n in waiting or None):
            return reminders.due(self.settings, now)

    def test_remind_at(self):
        self.assertEqual(reminders.remind_at(at(16, 50), at(19)), at(19))
        self.assertEqual(reminders.remind_at(at(16, 50), None), at(17, 20))

    def test_not_due_during_calls(self):
        self.assertEqual(self.due(at(18, 30)), [])

    def test_due_once_after_calls_and_skips_reviewed_prs(self):
        result = self.due(at(19, 5), waiting=(2,))
        self.assertEqual([(cid, [sg.pr.number for sg in p]) for cid, p in result], [(self.card, [2])])
        statuses = {r["pr_number"]: r["status"] for r in store.card_suggestions(self.card)}
        self.assertEqual(statuses, {1: "stale", 2: "pending"})
        self.assertEqual(self.due(at(19, 10)), [])

    def test_no_reminder_once_everything_is_decided(self):
        for row in store.card_suggestions(self.card):
            store.claim(row["id"], "U1")
            store.finish(row["id"], "kept")
        self.assertEqual(self.due(at(19, 5)), [])


def review(login: str, state: str, sha: str = "head", minute: int = 0):
    return mock.Mock(user=mock.Mock(login=login), state=state, commit_id=sha, submitted_at=at(12, minute))


class ApprovalGateTest(unittest.TestCase):
    def test_only_lead_unless_handed_off(self):
        self.assertEqual(approval_gate.allowed_approvers(TEAM, "andrei", set()), {"lead"})
        self.assertEqual(approval_gate.allowed_approvers(TEAM, "andrei", {"cone"}), {"lead", "cone"})

    def test_lead_pr_can_be_approved_by_any_teammate_but_not_the_author(self):
        self.assertEqual(approval_gate.allowed_approvers(TEAM, "lead", set()), {"andrei", "cone"})
        self.assertEqual(approval_gate.allowed_approvers(TEAM, "andrei", {"andrei"}), {"lead"})

    def test_only_latest_decisive_review_on_head_counts(self):
        reviews = [
            review("lead", "APPROVED", "old", 0),
            review("cone", "APPROVED", minute=1),
            review("cone", "COMMENTED", minute=2),
            review("andrei", "APPROVED", minute=3),
            review("andrei", "CHANGES_REQUESTED", minute=4),
        ]
        self.assertEqual(approval_gate.approvers_of_head(reviews, "head"), {"cone"})

    def test_evaluate(self):
        self.assertEqual(approval_gate.evaluate({"lead"}, {"cone"}), ("pending", "Needs approval from @lead"))
        self.assertEqual(approval_gate.evaluate({"lead", "cone"}, {"cone"}), ("success", "Approved by @cone"))


class LeadContextTest(unittest.TestCase):
    def ctx(self, events, now):
        s = mock.Mock(spec=Settings)
        s.team, s.precall_minutes = TEAM, 10
        blocks = [(e["start"], e["end"]) for e in events]
        with mock.patch.object(sweep.google_calendar, "timezone_of", return_value=TZ), \
             mock.patch.object(sweep.google_calendar, "events_between", return_value=events):
            return sweep.lead_context(s, now, blocks)

    def event(self, summary, start, end, review_block=False):
        return {"id": summary, "summary": summary, "start": start, "end": end, "review_block": review_block}

    def test_call_in_progress(self):
        c = self.ctx([self.event("Sync BCR", at(18), at(19))], at(18, 30))
        self.assertEqual((c.header, c.busy_until), ("📅 You're in *Sync BCR* until 19:00", at(19)))

    def test_review_block_is_not_a_call(self):
        c = self.ctx([self.event("🔍 Review PR #3: x", at(19), at(19, 30), True)], at(19, 10))
        self.assertEqual(c.header, "🔍 You're in a review block until 19:30")

    def test_upcoming_review_block_is_not_a_precall(self):
        c = self.ctx([self.event("🔍 Review PR #3: x", at(19), at(19, 30), True)], at(18, 55))
        self.assertEqual((c.header, c.upcoming_event), ("No calls right now", None))


class ReviewerPickTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()
        self.settings = mock.Mock(spec=Settings)
        self.settings.team = TEAM
        busy = {"cone@x.com": [(at(16), at(18))]}
        team = Team(LEAD, (LEAD, ANDREI, CONE, Member("Ana", "ana", "ana@x.com")))
        pr = make_pr(author="ana")
        self.ranked = ranking.rank(pr, team, Counter(cone=4), Counter(), busy, NOW)
        self.sg = ranking.decide(pr, HIGH, self.ranked, (at(19), at(19, 30)), at(19))
        self.card = store.create_card("manual", "h", [self.sg])
        self.sid = store.card_suggestions(self.card)[0]["id"]

    def test_ranking_survives_storage(self):
        back = Suggestion.from_json(store.suggestion(self.sid)["payload"])
        self.assertEqual([c.github for c in back.candidates], ["andrei", "cone"])
        self.assertEqual(back.candidates[1].busy_until, at(18))

    def test_picker_lists_ranking_with_reasons(self):
        picker = blocks.reviewer_picker(self.sg, "1", TZ)
        labels = [o["text"]["text"] for o in picker["options"]]
        self.assertEqual(labels, ["1. Andrei · free", "2. Cone · busy until 18:00"])
        self.assertEqual(blocks.ranking_line(self.sg, TZ),
                         "👥 *Ranking*\n1. *Andrei*: free · 0 commits here · 0 open reviews\n"
                         "2. *Cone*: busy until 18:00 · 4 commits here · 0 open reviews")
        self.assertEqual(picker["action_id"], "radar:pick:1")

    def test_pick_must_be_in_ranking(self):
        self.assertFalse(actions.execute(self.settings, self.sid, "pick", "U1", reviewer="lead"))
        self.assertEqual(store.suggestion(self.sid)["status"], "pending")

    def test_pick_hands_off_to_chosen_engineer_even_on_high_risk(self):
        with mock.patch.object(actions.google_calendar, "timezone_of", return_value=TZ), \
             mock.patch.object(actions.github, "live_repo"), \
             mock.patch.object(actions.github, "still_waiting_on"), \
             mock.patch.object(actions.github, "hand_off") as hand_off:
            self.assertTrue(actions.execute(self.settings, self.sid, "pick", "U1", reviewer="cone"))
        hand_off.assert_called_once()
        self.assertEqual(hand_off.call_args.args[1:3], ("lead", "cone"))
        row = store.suggestion(self.sid)
        self.assertEqual((row["status"], row["result"]), ("handed_off", "Handed off to Cone (your pick)"))
        # The merge gate must follow the lead's pick, also on a high-risk PR Radar had no reviewer for.
        self.assertEqual(approval_gate._handed_off_to(self.sg.pr.number), {"cone"})

    def _pick(self, on_handoff):
        with mock.patch.object(actions.google_calendar, "timezone_of", return_value=TZ), \
             mock.patch.object(actions.github, "live_repo"), \
             mock.patch.object(actions.github, "still_waiting_on"), \
             mock.patch.object(actions.github, "hand_off"):
            return actions.execute(self.settings, self.sid, "pick", "U1", reviewer="cone", on_handoff=on_handoff)

    def test_reviewer_is_notified_after_handoff(self):
        notify = mock.Mock()
        self._pick(notify)
        notify.assert_called_once_with(self.sg, "cone")

    def test_failed_notification_does_not_undo_handoff(self):
        self._pick(mock.Mock(side_effect=RuntimeError("slack down")))
        self.assertEqual(store.suggestion(self.sid)["status"], "handed_off")


class DigestTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()

    def test_last_stretch_merges_back_to_back_calls(self):
        blocks_ = [(at(17), at(18)), (at(18), at(18, 30)), (at(19), at(20))]
        self.assertEqual(digest.last_stretch(blocks_, at(18, 35)), (at(17), at(18, 30)))
        self.assertIsNone(digest.last_stretch(blocks_, at(16)))

    def test_only_actions_for_that_stretch(self):
        reviewer = Candidate("cone", "Cone", True, 0, 0)
        during = Suggestion(make_pr(1), LOW, "handoff", "w", reviewer, None, at(18, 30))
        earlier = Suggestion(make_pr(2), LOW, "handoff", "w", reviewer, None, at(12))
        undecided = Suggestion(make_pr(3), HIGH, "block", "w", None, (at(18, 30), at(19)), at(18, 30))
        card = store.create_card("precall", "h", [during, earlier, undecided])
        for row in store.card_suggestions(card)[:2]:
            store.claim(row["id"], "U1")
            store.finish(row["id"], "handed_off", handed_to="cone")
        acted = digest.acted_during(store.all_suggestions(), at(17), at(18, 30))
        self.assertEqual([sg.pr.number for _, sg in acted], [1])

    def test_review_outcome_uses_latest_review_after_handoff(self):
        reviews = [review("cone", "COMMENTED", minute=0), review("cone", "APPROVED", minute=5),
                   review("andrei", "APPROVED", minute=6)]
        self.assertEqual(digest.review_outcome(reviews, "cone", at(11)), "APPROVED")
        self.assertIsNone(digest.review_outcome(reviews, "cone", at(13)))

    def test_lines_and_message(self):
        self.assertEqual(digest.handoff_line("Cone", "APPROVED", False, False), ("✅", "Cone approved it"))
        self.assertEqual(digest.handoff_line("Ana", None, False, False), ("👀", "Ana is on it (no review yet)"))
        lines = [digest.Line(1, "Fix typo", "https://x/1", "✅", "Cone approved it")]
        self.assertEqual(messages.digest_text((at(17), at(18, 30)), lines, TZ),
                         "☕ *You're out of calls.* While you were busy (17:00–18:30), Radar's handoffs did this:\n"
                         "✅ <https://x/1|#1 Fix typo>: Cone approved it")

    def test_handoff_dm_text(self):
        s = mock.Mock(spec=Settings)
        s.team = TEAM
        sg = Suggestion(make_pr(1), LOW, "handoff", "w", None, None, at(18, 30))
        text = messages.handoff_text(s, sg, TZ)
        self.assertIn("*Samson handed you a review* (busy until 18:30)", text)
        self.assertIn("*<https://github.com/o/r/pull/1|#1 PR 1>* by Andrei", text)
        self.assertIn("🟢 Low risk · look first: `README.md`", text)


if __name__ == "__main__":
    unittest.main()

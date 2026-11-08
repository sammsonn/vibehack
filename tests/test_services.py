"""radar.services: triage, reminders, merge gate, sweep context and approved actions (external calls mocked)."""
import datetime as dt
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from radar.config import Member, Settings, Team
from radar.core import ranking
from radar.core.models import Candidate, Suggestion, TokenUsage
from radar.integrations import llm
from radar.services import actions, approval_gate, baseline, digest, reminders, review_alerts, sweep, triage
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
        with mock.patch.object(triage.llm, "ask", return_value=({"summary": "s", "risk": "low", "risk_reason": "r", "look_first": "f"}, TokenUsage())):
            self.assertEqual(triage.triage(self.settings, pr).risk, "high")

    def test_falls_back_to_rules_without_llm(self):
        with mock.patch.object(triage.llm, "ask", return_value=(None, TokenUsage())):
            t = triage.triage(self.settings, make_pr(files=("migrations/1.sql",)))
        self.assertEqual((t.risk, t.summary), ("high", "PR 1"))


class TokenUsageTest(unittest.TestCase):
    def reply(self, content, prompt=100, completion=40):
        return mock.Mock(choices=[mock.Mock(message=mock.Mock(content=content))],
                         usage=mock.Mock(prompt_tokens=prompt, completion_tokens=completion))

    def test_ask_counts_tokens_of_every_attempt(self):
        good = '{"summary": "s", "risk": "low", "risk_reason": "r", "look_first": "f"}'
        settings = mock.Mock(spec=Settings, llm_base_url="u", llm_api_key="k", llm_model="m")
        with mock.patch.object(llm, "OpenAI") as client:
            client.return_value.chat.completions.create.side_effect = [self.reply("not json"), self.reply(good, 50, 10)]
            data, usage = llm.ask(settings, make_pr())
        self.assertEqual(data["risk"], "low")
        self.assertEqual(usage, TokenUsage(150, 50))

    def test_triage_records_usage_only_when_tokens_were_spent(self):
        settings = mock.Mock(spec=Settings, llm_model="m")
        with mock.patch.object(triage.llm, "ask", return_value=(None, TokenUsage(120, 30))), \
             mock.patch.object(triage.store, "record_llm_usage") as record:
            triage.triage(settings, make_pr(7))
            record.assert_called_once_with("m", 7, TokenUsage(120, 30))
        with mock.patch.object(triage.llm, "ask", return_value=(None, TokenUsage())), \
             mock.patch.object(triage.store, "record_llm_usage") as record:
            triage.triage(settings, make_pr(7))
            record.assert_not_called()


class ReviewBlockLookupTest(unittest.TestCase):
    def test_pr_number_from_tag_or_title(self):
        from radar.integrations import google_calendar as g
        self.assertEqual(g._block_pr({"extendedProperties": {"private": {"radar": "review-block", "pr": "8"}}}), 8)
        self.assertEqual(g._block_pr({"summary": "🔍 Review PR #5: Verify Stripe webhook signatures"}), 5)
        self.assertIsNone(g._block_pr({"summary": "Sync BCR"}))

    def test_earliest_unfinished_block_per_pr(self):
        from radar.integrations import google_calendar as g
        ev = lambda pr, s, e, rb=True: {"pr": pr, "start": s, "end": e, "review_block": rb}  # noqa: E731
        events = [ev(5, at(16), at(16, 30)), ev(5, at(19, 30), at(20)), ev(5, at(21), at(21, 30)),
                  ev(8, at(20), at(20, 30)), ev(None, at(17), at(18), False)]
        with mock.patch.object(g, "events_between", return_value=events):
            self.assertEqual(g.upcoming_review_blocks("lead@x.com", NOW), {5: (at(19, 30), at(20)), 8: (at(20), at(20, 30))})


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


class ExistingBlockTest(unittest.TestCase):
    def test_approving_an_old_card_reuses_the_booked_block(self):
        with mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db"):
            store.init()
            sg = Suggestion(make_pr(5), HIGH, "block", "High risk", None, (at(19), at(19, 30)), at(19))
            card = store.create_card("manual", "h", [sg])
            sid = store.card_suggestions(card)[0]["id"]
            settings = mock.Mock(spec=Settings)
            settings.team = TEAM
            with mock.patch.object(actions.google_calendar, "timezone_of", return_value=TZ), \
                 mock.patch.object(actions.google_calendar, "upcoming_review_blocks", return_value={5: (at(19, 30), at(20))}), \
                 mock.patch.object(actions.google_calendar, "create_review_block") as create, \
                 mock.patch.object(actions.github, "live_repo"), \
                 mock.patch.object(actions.github, "still_waiting_on"):
                self.assertTrue(actions.execute(settings, sid, "approve", "U1"))
            create.assert_not_called()
            row = store.suggestion(sid)
            self.assertEqual(row["status"], "blocked")
            self.assertIn("already booked", row["result"])


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


class ReviewAlertTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()
        self.settings = mock.Mock(spec=Settings)
        self.settings.team = TEAM
        # Handed off on a card made while the lead was free (no busy stretch).
        self.sg = Suggestion(make_pr(1), LOW, "handoff", "w", Candidate("cone", "Cone", True, 0, 0), None, None)
        card = store.create_card("manual", "h", [self.sg])
        sid = store.card_suggestions(card)[0]["id"]
        store.claim(sid, "U1")
        store.finish(sid, "handed_off", handed_to="cone")
        self.pr = mock.Mock(number=1, head=mock.Mock(sha="head"))

    def rev(self, rid, login, state, when, sha="head"):
        return mock.Mock(id=rid, user=mock.Mock(login=login), state=state, commit_id=sha, submitted_at=when)

    def check(self, reviews, now, busy=()):
        notify = mock.Mock()
        with mock.patch.object(review_alerts.google_calendar, "busy_blocks",
                               return_value={"lead@x.com": list(busy)}):
            review_alerts.check(self.settings, self.pr, reviews, notify, now=now)
        return notify

    def decided_at(self):
        return dt.datetime.fromisoformat(store.all_suggestions()[0]["decided_at"])

    def test_alerts_once_per_review_by_the_handed_off_reviewer(self):
        when = self.decided_at()
        reviews = [self.rev(7, "cone", "APPROVED", when), self.rev(8, "andrei", "APPROVED", when)]
        notify = self.check(reviews, when)
        notify.assert_called_once_with(self.sg, "cone", "APPROVED", True)
        self.check(reviews, when).assert_not_called()

    def test_waits_while_lead_is_in_a_call(self):
        when = self.decided_at()
        reviews = [self.rev(7, "cone", "CHANGES_REQUESTED", when)]
        busy = [(when - dt.timedelta(hours=1), when + dt.timedelta(hours=1))]
        self.check(reviews, when, busy).assert_not_called()
        self.assertFalse(store.was_fired(digest.review_key(1, 7)))

    def test_digest_covers_reviews_during_its_stretch(self):
        stretch = (at(17), at(18, 30))
        self.assertFalse(review_alerts.announce_now(at(18), True, False, stretch, at(18, 35)))
        self.assertTrue(review_alerts.announce_now(at(18, 40), True, False, stretch, at(18, 41)))
        self.assertTrue(review_alerts.announce_now(at(18), True, False, stretch, at(19, 10)))
        self.assertTrue(review_alerts.announce_now(at(18), False, False, stretch, at(18, 35)))

    def test_alert_text(self):
        self.assertEqual(messages.review_alert_text(self.settings, self.sg, "cone", "APPROVED", True),
                         "✅ *Cone approved* <https://github.com/o/r/pull/1|#1 PR 1>: the merge is unblocked.")
        self.assertEqual(messages.review_alert_text(self.settings, self.sg, "cone", "CHANGES_REQUESTED", False),
                         "✍️ *Cone requested changes on* <https://github.com/o/r/pull/1|#1 PR 1>: back with the author.")


class BaselineTest(unittest.TestCase):
    def pr(self, n, author, opened_h, review_h, reviewer, requested=(), merged=True, open_=False):
        first = at(opened_h) + dt.timedelta(hours=review_h) if review_h is not None else None
        return baseline.PRHistory(n, author, at(opened_h), first, reviewer, frozenset(requested), merged, open_)

    def test_wait_concentration_and_lead_bottleneck(self):
        prs = [
            self.pr(1, "ana", 9, 6, "sam", {"sam"}),
            self.pr(2, "dan", 9, 4, "sam", {"sam"}),
            self.pr(3, "sam", 10, 1, "ana", {"ana"}),
            self.pr(4, "ana", 11, None, None, merged=True),
            self.pr(5, "dan", 12, None, None, {"sam"}, merged=False, open_=True),
        ]
        b = baseline.compute(prs, at(16))
        self.assertEqual((b.prs, b.authors, b.reviewed), (5, 3, 3))
        self.assertEqual(b.median, dt.timedelta(hours=4))
        self.assertEqual((b.merged_unreviewed, b.merged), (1, 4))
        self.assertEqual((b.waiting, b.waiting_median), (1, dt.timedelta(hours=4)))
        self.assertEqual((b.top_reviewer, round(b.top_share, 2)), ("sam", 0.67))
        self.assertEqual((b.wait_top_requested, b.wait_others_requested), (dt.timedelta(hours=5), dt.timedelta(hours=1)))
        self.assertIn("PRs waited 4h 00m for a first review; 67% of first reviews came from one person",
                      baseline.render(b, "o/r", 90))

    def test_backtest_hands_off_only_low_risk_prs_waiting_on_the_bottleneck(self):
        def pr(n, author, review_h, reviewer, requested, files):
            return baseline.PRHistory(n, author, at(9), at(9) + dt.timedelta(hours=review_h), reviewer,
                                      frozenset(requested), True, False, files, 10)
        prs = [
            pr(1, "ana", 10, "sam", {"sam"}, ("README.md",)),          # low risk on sam -> handed off
            pr(2, "dan", 8, "sam", {"sam"}, ("payments/api.py",)),     # high risk -> stays with sam
            pr(3, "ana", 2, "dan", {"dan"}, ("src/a.py",)),            # asked someone else -> unchanged
            pr(4, "dan", 1, "ana", {"ana"}, ("src/b.py",)),
            pr(5, "ana", 3, "dan", {"dan"}, ("src/c.py",)),
            pr(6, "dan", 4, "ana", {"ana"}, ("src/d.py",)),
            pr(7, "ana", 0.5, "sam", {"sam"}, ("docs.md",)),           # already faster than a handoff -> unchanged
        ]
        pool = baseline.others_waits(prs, "sam")
        self.assertEqual(sorted(pool), [dt.timedelta(hours=h) for h in (1, 2, 3, 4)])
        pj = baseline.project(prs, "sam", 1.0, [dt.timedelta(hours=1)], runs=10)
        self.assertEqual((pj.on_bottleneck, pj.handed_off, pj.kept_risky), (3, 2, 1))
        self.assertEqual(pj.risk_reasons, Counter(payments=1))
        # PR 1 drops from 10h to 1h; PR 7 gets slower (0.5h -> 1h): handoffs aren't assumed to always help.
        self.assertEqual(pj.hours_removed, 9 - 0.5)
        self.assertEqual(baseline.project(prs, "sam", 0.0, pool, runs=10).hours_removed, 0)


if __name__ == "__main__":
    unittest.main()

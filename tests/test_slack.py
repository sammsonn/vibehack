"""radar.slack: card rendering, posting and the /radar command."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from radar.core.models import Candidate, Suggestion
from radar.slack import blocks, bot, messages
from radar.storage import store
from tests.helpers import HIGH, LOW, NOW, TEAM, TZ, at, make_pr


class RenderTest(unittest.TestCase):
    def test_pending_card_has_buttons_and_approve_all(self):
        handoff = Suggestion(make_pr(1), LOW, "handoff", "why", Candidate("cone", "Cone", True, 3, 0), None, at(19))
        block = Suggestion(make_pr(3), HIGH, "block", "High risk", None, (at(19), at(19, 30)), at(19))
        card = {"id": 7, "created_at": NOW.isoformat(), "header": "📅 *Demo* starts in 10 min"}
        rows = [{"id": i, "payload": sg.to_json(), "status": "pending", "decided_by": None, "decided_at": None,
                 "snoozed_until": None, "result": None} for i, sg in enumerate([block, handoff], start=1)]
        _, kit = blocks.render(card, rows, TZ, TEAM)
        ids = [e["action_id"] for b in kit if b["type"] == "actions" for e in b["elements"]]
        self.assertIn("radar:approve:1", ids)
        self.assertIn("radar:all:7", ids)
        texts = [b["text"]["text"] for b in kit if b["type"] == "section"]
        self.assertTrue(any("(Andrei, " in t for t in texts))
        self.assertFalse(any("@andrei" in t for t in texts))
        labels = [e["text"]["text"] for b in kit if b["type"] == "actions" for e in b["elements"]]
        self.assertIn("📅 Block 19:00–19:30 to review", labels)
        self.assertIn("✅ Hand off to Cone", labels)


def row(i, sg, status="pending", decided_at=None):
    return {"id": i, "payload": sg.to_json(), "status": status, "decided_by": None, "decided_at": decided_at,
            "snoozed_until": None, "result": None}


class ReplacedRowsTest(unittest.TestCase):
    card = {"id": 3, "created_at": NOW.isoformat(), "header": "h"}
    handoff = Suggestion(make_pr(1), LOW, "handoff", "why", Candidate("cone", "Cone", True, 3, 0), None, at(19))
    block = Suggestion(make_pr(3), HIGH, "block", "High risk", None, (at(19), at(19, 30)), at(19))

    def test_replaced_row_is_greyed_out_without_buttons(self):
        rows = [row(1, self.block, "superseded", at(19, 32).isoformat()), row(2, self.handoff)]
        _, kit = blocks.render(self.card, rows, TZ, TEAM)
        self.assertIn("1 PR is waiting on you. 1 moved to a newer card.", kit[0]["text"]["text"])
        grey = [e["text"] for b in kit if b["type"] == "context" for e in b["elements"] if "~" in e["text"]]
        self.assertEqual(grey, ["🔴 ~<https://github.com/o/r/pull/3|#3 PR 3>~ · ↪️ Moved to a newer card at 19:32"])
        action_ids = [e["action_id"] for b in kit if b["type"] == "actions" for e in b["elements"]]
        self.assertFalse(any(a.endswith(":1") for a in action_ids))  # no buttons for the replaced row
        self.assertIn("radar:approve:2", action_ids)
        self.assertNotIn("radar:all:3", action_ids)  # only one live row left

    def test_fully_replaced_card(self):
        rows = [row(1, self.block, "superseded", at(19, 32).isoformat()), row(2, self.handoff, "superseded", at(19, 32).isoformat())]
        _, kit = blocks.render(self.card, rows, TZ, TEAM)
        self.assertIn("All PRs on this card moved to a newer card.", kit[0]["text"]["text"])
        self.assertFalse([b for b in kit if b["type"] == "actions"])


class PostCardTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(store, "DB_PATH", Path(tempfile.mkdtemp()) / "t.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        store.init()
        self.settings = mock.Mock(team=TEAM)
        self.client = mock.Mock()
        self.client.conversations_open.return_value = {"channel": {"id": "D1"}}
        self.client.chat_postMessage.side_effect = [{"channel": "D1", "ts": "1.0"}, {"channel": "D1", "ts": "2.0"}]

    def post(self, card_id):
        with mock.patch.object(messages.google_calendar, "timezone_of", return_value=TZ), \
             mock.patch.object(messages, "slack_id", return_value="U1"):
            messages.post_card(self.client, self.settings, card_id)

    def test_new_card_greys_out_the_rows_it_took_over_on_the_old_card(self):
        old = store.create_card("precall", "h", [ReplacedRowsTest.handoff, ReplacedRowsTest.block])
        self.post(old)
        self.client.chat_update.assert_not_called()

        new = store.create_card("manual", "h", [ReplacedRowsTest.handoff])  # PR 1 again
        self.assertEqual(store.cards_superseded_by(new), [old])
        statuses = {r["pr_number"]: (r["status"], r["superseded_by"]) for r in store.card_suggestions(old)}
        self.assertEqual(statuses, {1: ("superseded", new), 3: ("pending", None)})

        self.post(new)
        self.client.chat_update.assert_called_once()
        self.assertEqual(self.client.chat_update.call_args.kwargs["ts"], "1.0")  # the old card's message
        old_blocks = self.client.chat_update.call_args.kwargs["blocks"]
        self.assertIn("1 moved to a newer card", old_blocks[0]["text"]["text"])

    def test_a_failing_update_of_the_old_card_does_not_break_posting(self):
        old = store.create_card("precall", "h", [ReplacedRowsTest.handoff])
        self.post(old)
        new = store.create_card("manual", "h", [ReplacedRowsTest.handoff])
        self.client.chat_update.side_effect = RuntimeError("message_not_found")
        self.post(new)
        self.assertEqual(store.card(new)["ts"], "2.0")


class CommandTest(unittest.TestCase):
    def run_command(self, text, report="stats text"):
        calls = []
        ack = mock.Mock(side_effect=lambda *a, **k: calls.append(("ack", a)))
        respond = mock.Mock(side_effect=lambda *a, **k: calls.append(("respond", a, k)))
        with mock.patch.object(bot.stats, "report", side_effect=lambda s, days: calls.append(("report", days)) or report):
            bot.handle_command(mock.Mock(), text.split(), ack, respond, post=mock.Mock())
        return calls

    def test_stats_acknowledges_before_the_slow_report(self):
        calls = self.run_command("stats 7")
        self.assertEqual(calls[0], ("ack", ("📊 Crunching the numbers for the last 7 days…",)))
        self.assertEqual(calls[1], ("report", 7))
        self.assertEqual(calls[2], ("respond", (), {"response_type": "in_channel", "text": "stats text"}))

    def test_bad_days_gets_usage_without_running_the_report(self):
        calls = self.run_command("stats abc")
        self.assertEqual(calls, [("ack", (bot.USAGE,))])

    def test_unknown_subcommand_gets_usage(self):
        self.assertEqual(self.run_command("hello"), [("ack", (bot.USAGE,))])


if __name__ == "__main__":
    unittest.main()

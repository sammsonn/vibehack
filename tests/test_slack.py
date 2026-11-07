"""radar.slack: card rendering."""
import unittest

from radar.core.models import Candidate, Suggestion
from unittest import mock

from radar.slack import blocks, bot
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

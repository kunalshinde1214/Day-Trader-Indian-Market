import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import Block
from replay_sampling import policy, draw_slots


class SamplingTests(unittest.TestCase):
    pool = [[(slot, session) for session in range(7)] for slot in range(11)]

    def draw(self, mode, **kwargs):
        return list(draw_slots(self.pool, random.Random(19), policy(
            {"replay_seed": 19, "replay_sampling": mode, **kwargs})))

    def test_default_matches_original_choice_sequence(self):
        rng = random.Random(19)
        self.assertEqual(self.draw("independent"), [rng.choice(slot) for slot in self.pool])

    def test_single_bar_blocks_match_independent(self):
        self.assertEqual(self.draw("independent"), self.draw("block", replay_block_bars=1))

    def test_blocks_preserve_sessions_and_clock_without_wrapping(self):
        rows = self.draw("block", replay_block_bars=4)
        self.assertEqual([r[0] for r in rows], list(range(11)))
        for start in (0, 4, 8):
            self.assertEqual(len({r[1] for r in rows[start:start+4]}), 1)
        self.assertGreater(len({r[1] for r in rows}), 1)

    def test_session_preserves_entire_tail(self):
        rows = self.draw("session")
        self.assertEqual(len({r[1] for r in rows}), 1)
        self.assertEqual(rows, self.draw("session"))

    def test_long_block_truncates_at_tail_end(self):
        self.assertEqual(self.draw("session"), self.draw("block", replay_block_bars=375))

    def test_invalid_policies_fail_closed(self):
        cases = [{"replay_sampling": "unknown"}, {"replay_block_bars": 6}]
        cases += [{"replay_sampling": "block", "replay_block_bars": x} for x in (0, -1, 376, True, 2.5, "6")]
        cases += [{"replay_seed": x} for x in (True, "19", 19.5, None)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(Block):
                policy({"replay_seed": 19, **case})

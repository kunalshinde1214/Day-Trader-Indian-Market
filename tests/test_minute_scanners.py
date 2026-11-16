"""Fictional minute bars exercise scanner clocks; no resampling of real market data."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from core import Block, setup_hit, plan_from_hit, size, ticket, instant
from demo import inputs
from entry_policy import check, check_candles
from replay import replay
from test_entry_intervals import minute_bars

RULES = ('vwap_reclaim', 'gap_and_go', 'breakout_20d', 'bull_flag', 'oversold_bounce')


def fixture(setup, broker='kite'):
    config = json.loads((ROOT / 'assets/config.example.json').read_text())
    config.update(setup=setup, broker=broker, interval_minutes=1, replay_paths=20)
    card = json.loads((ROOT / 'assets/charges.example.json').read_text()); card['broker'] = broker
    now, instrument, context, history, _, candles = inputs(config)
    data = candles['instruments']['NSE:DEMO-EQ']
    rows = minute_bars(data['bars'])
    if setup != 'gap_and_go':
        for row in rows[15:19]:
            row.update(open=99, high=100, low=98, close=99) if setup == 'vwap_reclaim' else row.update(
                open=100.5, high=100.9, low=100.4, close=100.5)
    data.update(bars=rows, interval='minute')
    history.update(bars=minute_bars(history['bars']), interval='minute')
    return config, card, now, instrument, context, history, data


def hit(config, now, instrument, data):
    return setup_hit(data['bars'], now, config, instrument, data.get('prior_close'), data.get('daily_reference'))


class MinuteScannerTests(unittest.TestCase):
    def test_all_five_rules_complete_paper_pipeline_across_five_brokers(self):
        for broker in ('kite', 'upstox', 'dhan', 'angel', 'fyers'):
            for setup in RULES:
                with self.subTest(broker=broker, setup=setup):
                    c, card, now, i, context, history, data = fixture(setup, broker)
                    observed = hit(c, now, i, data)
                    self.assertEqual(observed['state'], 'triggered')
                    self.assertEqual(observed['trigger_at'], now.isoformat())
                    self.assertEqual(observed['interval_minutes'], 1)
                    plan = plan_from_hit(observed, i, c, now, rows=data['bars'])
                    context['replay'] = replay(plan, i, history, c, card)
                    result = ticket(size(plan, i, c, context, card, now), i, c, context, card, now)
                    self.assertEqual(result['risk']['status'], 'PASS')
                    self.assertFalse(result['submitted'])

    def test_forming_minute_cannot_trigger_any_rule(self):
        for setup in RULES:
            c, _, now, i, _, _, data = fixture(setup)
            with self.subTest(setup=setup):
                self.assertEqual(hit(c, now - timedelta(seconds=1), i, data)['state'], 'waiting')

    def test_missing_minute_is_not_interpolated(self):
        for setup in RULES:
            c, _, now, i, _, _, data = fixture(setup)
            del data['bars'][7]
            with self.subTest(setup=setup), self.assertRaisesRegex(Block, 'BARS'):
                hit(c, now, i, data)

    def test_gap_still_requires_fifteen_clock_minutes(self):
        c, _, now, i, _, _, data = fixture('gap_and_go')
        for minute in (18, 20, 25, 29):
            self.assertEqual(hit(c, now.replace(minute=minute), i, data)['state'], 'waiting')
        self.assertEqual(instant(hit(c, now, i, data)['trigger_at']).strftime('%H:%M'), '09:30')

    def test_gap_touch_any_opening_minute_cancels_continuation(self):
        for index in (0, 7, 14):
            c, _, now, i, _, _, data = fixture('gap_and_go')
            data['bars'][index]['low'] = 100
            self.assertEqual(hit(c, now, i, data)['state'], 'waiting')

    def test_gap_down_retains_symmetric_direction(self):
        c, _, now, i, _, _, data = fixture('gap_and_go')
        for row in data['bars']:
            row.update(open=200-row['open'], high=200-row['low'], low=200-row['high'], close=200-row['close'])
        self.assertEqual(hit(c, now, i, data)['side'], 'SELL')

    def test_interval_change_requires_new_plan_and_history(self):
        for setup in RULES:
            c, card, now, i, _, history, data = fixture(setup)
            plan = plan_from_hit(hit(c, now, i, data), i, c, now)
            with self.assertRaises(Block): check(plan, {**c, 'interval_minutes': 5})
            history['interval'] = '5minute'
            with self.assertRaisesRegex(Block, 'INTERVAL'): replay(plan, i, history, c, card)
            with self.assertRaisesRegex(Block, 'INTERVAL'): check_candles({'interval': '5minute'}, c)

    def test_later_bar_does_not_rewrite_trigger_evidence(self):
        for setup in RULES:
            c, _, now, i, _, _, data = fixture(setup)
            expected = hit(c, now, i, data)
            data['bars'].append({'at': now.isoformat(), 'open': 102, 'high': 200, 'low': 1, 'close': 150, 'volume': 999999})
            self.assertEqual(hit(c, now + timedelta(minutes=1), i, data), expected)

    def test_vwap_keeps_volume_weighting_and_rejects_zero_volume(self):
        c, _, now, i, _, _, data = fixture('vwap_reclaim')
        data['bars'][0]['volume'] *= 3
        observed = hit(c, now, i, data)
        expected = sum((b['high'] + b['low'] + b['close']) / 3 * b['volume'] for b in data['bars']) / sum(b['volume'] for b in data['bars'])
        self.assertAlmostEqual(observed['vwap'], expected)
        self.assertIn('1-minute', observed['vwap_basis'])
        data['bars'][4]['volume'] = 0
        with self.assertRaises(Block): hit(c, now, i, data)

    def test_daily_reference_cannot_use_information_from_target_session(self):
        for setup in RULES[2:]:
            c, _, now, i, _, _, data = fixture(setup)
            data['daily_reference']['bars'][-1]['available_at'] = now.isoformat()
            with self.assertRaisesRegex(Block, 'DAILY_REFERENCE'): hit(c, now, i, data)


if __name__ == '__main__': unittest.main()

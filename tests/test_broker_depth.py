"""Synthetic full-quote contracts; no authenticated accounts or network."""
import copy
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Block
from broker import adapter
from broker_data import quote_key
from depth_collect import observation, combine, collect
from liquidity import evidence
from test_execution import sample, NOW


def fixture(broker, stamp=NOW, volume=1000):
    instrument, _ = sample(broker)
    instrument.update(token={'upstox': 'NSE_EQ|INE000000001', 'dhan': '100',
                             'angel': '100', 'fyers': '10100000999999'}[broker], symbol='DEMO-EQ')
    book = {'buy': [{'price': 99, 'quantity': 50}, {'price': 0, 'quantity': 0}],
            'sell': [{'price': 101, 'quantity': 70}]}
    key = quote_key(instrument)
    if broker == 'upstox':
        payloads = [{'arbitrary-key': {'instrument_token': instrument['token'], 'last_price': 100,
                     'timestamp': stamp.isoformat(), 'depth': book, 'volume': volume}}]
    elif broker == 'dhan':
        payloads = [{'status': 'success', 'data': {'NSE_EQ': {'100': {'last_price': 100,
                     'last_trade_time': stamp.strftime('%d/%m/%Y %H:%M:%S'), 'depth': book, 'volume': volume}}}}]
    elif broker == 'angel':
        payloads = [{'fetched': [{'exchange': 'NSE', 'symbolToken': '100', 'tradingSymbol': 'DEMO-EQ',
                     'ltp': 100, 'exchTradeTime': stamp.strftime('%d-%b-%Y %H:%M:%S'),
                     'exchFeedTime': stamp.strftime('%d-%b-%Y %H:%M:%S'), 'tradeVolume': volume,
                     'depth': book}], 'unfetched': []}]
    else:
        payloads = [{'d': [{'n': key, 's': 'ok', 'v': {'fyToken': instrument['token'], 'symbol': key}}]},
                    {'d': {key: {'ltp': 100, 'ltt': int(stamp.timestamp()), 'v': volume,
                         'bids': [{'price': 99, 'volume': 50}], 'ask': [{'price': 101, 'volume': 70}]}}}]
    client = adapter(broker)
    client.request = Mock(side_effect=payloads)
    return instrument, client


def response(broker, stamp=NOW, volume=1000):
    i, client = fixture(broker, stamp, volume)
    return i, client.quotes([quote_key(i)])


class BrokerDepthTests(unittest.TestCase):
    def test_all_four_provider_books_preserve_units_and_volume(self):
        for broker in ('upstox', 'dhan', 'angel', 'fyers'):
            with self.subTest(broker=broker):
                i, result = response(broker)
                observed = observation(result, i, NOW)
                self.assertEqual(observed['bids'], [{'price': 99, 'quantity': 50}])
                self.assertEqual(observed['asks'], [{'price': 101, 'quantity': 70}])
                self.assertEqual(observed['cumulative_volume'], 1000)
                self.assertEqual(observed['source'], broker)
                self.assertEqual(observed['liquidity_model_eligible'], broker in {'upstox', 'angel'})

    def test_two_samples_use_delta_and_retain_time_limitations(self):
        for broker in ('upstox', 'dhan', 'angel', 'fyers'):
            i, first = response(broker)
            later = NOW + timedelta(seconds=2)
            _, second = response(broker, later, 1030)
            client = Mock(); client.quotes.side_effect = [first, second]
            result = collect(client, i, clock=Mock(side_effect=[NOW, later]), sleep=Mock())
            self.assertEqual(result['traded_units'], 30)
            self.assertEqual(bool(result['limitations']), broker in {'dhan', 'fyers'})

    def test_last_trade_only_books_cannot_enter_live_liquidity_model(self):
        for broker in ('dhan', 'fyers'):
            i, raw = response(broker)
            book = observation(raw, i, NOW)
            config = {'broker': broker, 'snapshot_max_age_seconds': 30,
                      'liquidity_model': {'mode': 'depth_stress_v1', 'depth_fraction': .5,
                        'max_participation_fraction': .1, 'exit_shock_bps': 10, 'max_age_seconds': 30}}
            with self.assertRaisesRegex(Block, 'book|depth freshness'):
                evidence({}, i, config, {'liquidity': book}, NOW)

    def test_feed_timed_book_integrates_with_liquidity_model(self):
        i, raw = response('upstox')
        _, raw2 = response('upstox', NOW + timedelta(seconds=2), 1100)
        book = combine(observation(raw, i, NOW), observation(raw2, i, NOW + timedelta(seconds=2)))
        config = {'broker': 'upstox', 'snapshot_max_age_seconds': 30,
                  'liquidity_model': {'mode': 'depth_stress_v1', 'depth_fraction': .5,
                    'max_participation_fraction': .1, 'exit_shock_bps': 10, 'max_age_seconds': 30}}
        result = evidence({'side': 'BUY', 'entry': 101}, i, config, {'liquidity': book}, NOW + timedelta(seconds=2))
        self.assertEqual(result['capacity'], 10)

    def test_stale_or_future_feed_times_block(self):
        i, raw = response('angel')
        for offset in (-31, 1):
            changed = copy.deepcopy(raw)
            changed['quotes'][quote_key(i)]['exchange_feed_time'] = (NOW + timedelta(seconds=offset)).isoformat()
            with self.subTest(offset=offset), self.assertRaises(Block): observation(changed, i, NOW)

    def test_feed_cannot_precede_last_trade(self):
        i, raw = response('angel')
        raw['quotes'][quote_key(i)]['exchange_feed_time'] = (NOW - timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(Block, 'precedes'): observation(raw, i, NOW)

    def test_fresh_fetch_does_not_refresh_old_trade(self):
        for broker in ('dhan', 'fyers'):
            i, raw = response(broker, NOW - timedelta(seconds=31))
            raw['fetched_at'] = NOW.isoformat()
            with self.assertRaises(Block): observation(raw, i, NOW)

    def test_stale_master_and_wrong_token_block(self):
        for broker in ('upstox', 'dhan', 'angel', 'fyers'):
            i, raw = response(broker)
            raw['quotes'][quote_key(i)]['token'] = 'wrong'
            with self.assertRaises(Block): observation(raw, i, NOW)
            i, raw = response(broker); i['master_date'] = '2020-01-01'
            with self.assertRaises(Block): observation(raw, i, NOW)

    def test_book_faults_block(self):
        for book in ({'buy': [], 'sell': [{'price': 101, 'quantity': 1}]},
                     {'buy': [{'price': 102, 'quantity': 1}], 'sell': [{'price': 101, 'quantity': 1}]},
                     {'buy': [{'price': 99, 'quantity': 1}] * 2, 'sell': [{'price': 101, 'quantity': 1}]},
                     {'buy': [{'price': 99.001, 'quantity': 1}], 'sell': [{'price': 101, 'quantity': 1}]},
                     {'buy': [{'price': 99, 'quantity': -1}], 'sell': [{'price': 101, 'quantity': 1}]}):
            i, raw = response('upstox')
            raw['quotes'][quote_key(i)]['depth_snapshot']['depth'] = book
            with self.subTest(book=book), self.assertRaises(Block): observation(raw, i, NOW)

    def test_timestamp_semantics_cannot_change_between_samples(self):
        i, raw = response('upstox'); first = observation(raw, i, NOW)
        second = {**first, 'as_of': (NOW + timedelta(seconds=2)).isoformat(), 'liquidity_model_eligible': False}
        with self.assertRaisesRegex(Block, 'semantics'): combine(first, second)

    def test_derivatives_rejected_before_any_broker_read(self):
        i, client = fixture('dhan'); i['kind'] = 'future'
        with self.assertRaises(Block): collect(client, i, sleep=Mock())
        client.request.assert_not_called()


if __name__ == '__main__': unittest.main()

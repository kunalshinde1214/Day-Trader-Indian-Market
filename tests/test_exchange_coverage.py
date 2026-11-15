import copy
import csv
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from core import Block, instant
import exchange_coverage as ex


class ExchangeCoverageTests(unittest.TestCase):
    now = instant('2026-11-15T10:00:00+05:30')

    def catalogue(self):
        return {'PreviousDay': [self.row('12-Nov-2026', '121126')],
                'CurrentDay': [self.row('13-Nov-2026', '131126')], 'FutureDay': [],
                'currentDate': '13-Nov-2026', 'previousDate': '12-Nov-2026', 'futureDate': '16-Nov-2026'}

    def row(self, day, suffix, prefix='REG1_IND'):
        return {'fileKey': next(k for k, v in ex.REPORT_KEYS.items() if v == prefix),
                'fileSegment': 'CM', 'filePath': ex.BASES[0],
                'fileActlName': prefix + suffix + '.csv', 'tradingDate': day}

    def csv(self, changes=None, duplicate=False):
        headers = ['ScripCode', 'Symbol', 'Nse Exclusive', 'Status', 'Series', *ex.STAGES, 'Unsolicited_SMS']
        row = {h: '100' for h in headers}
        row.update(ScripCode='NA', Symbol='DEMO', Status='A', Series='EQ', **{'Nse Exclusive': 'N'})
        row.update(changes or {})
        stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader(); writer.writerow(row)
        if duplicate: writer.writerow(row)
        return stream.getvalue().encode()

    def descriptor(self):
        return ex.catalogue(json.dumps(self.catalogue()).encode(), self.now)

    def test_catalogue_latest_date_then_richer_format(self):
        data = self.catalogue()
        data['CurrentDay'].append(self.row('13-Nov-2026', '131126', 'REG_IND'))
        self.assertEqual(ex.catalogue(json.dumps(data).encode(), self.now)['format'], 'REG1_IND')
        data['CurrentDay'] = [self.row('14-Nov-2026', '141126', 'REG_IND')]
        self.assertEqual(ex.catalogue(json.dumps(data).encode(), self.now)['format'], 'REG_IND')

    def test_path_filename_date_and_segment_must_match(self):
        for changes in ({'filePath': 'https://attacker.invalid/'}, {'filePath': ex.BASES[0] + '../'},
                        {'fileActlName': 'REG1_IND121126.csv'}, {'fileSegment': 'FO'}, {'tradingDate': '16-Nov-2026'}):
            data = self.catalogue(); data['CurrentDay'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(Block):
                ex.catalogue(json.dumps(data).encode(), self.now)

    def test_catalogue_missing_stale_and_conflicting_fail(self):
        cases = [{}, {'CurrentDay': []}, {'CurrentDay': [self.row('01-Nov-2026', '011126')]}]
        data = self.catalogue(); other = copy.deepcopy(data['CurrentDay'][0]); other['filePath'] = ex.BASES[1]
        data['CurrentDay'].append(other); cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(Block):
                ex.catalogue(json.dumps(data).encode(), self.now)

    def test_surveillance_stage_zero_is_restricted_and_tft_series(self):
        report = ex.surveillance(self.csv({'GSM': '0', 'Series': 'BE'}), self.descriptor(), self.now)
        row = report['records']['DEMO:BE']
        self.assertEqual(row['stages']['gsm'], 0); self.assertTrue(row['trade_for_trade'])
        self.assertIsNone(row['stages']['esm'])
        self.assertFalse(report['effective_session_verified']); self.assertFalse(report['execution_eligible'])
        self.assertEqual(row['additional_source_fields']['Unsolicited_SMS'], '100')

    def test_unknown_stages_status_or_duplicate_security_fail(self):
        for changes in ({'GSM': ''}, {'GSM': '5'}, {'ESM': '999'}, {'Status': '?'}, {'Symbol': '<html>'}):
            with self.subTest(changes=changes), self.assertRaises(Block):
                ex.surveillance(self.csv(changes), self.descriptor(), self.now)
        with self.assertRaises(Block): ex.surveillance(self.csv(duplicate=True), self.descriptor(), self.now)

    def test_ragged_missing_duplicate_columns_and_empty_fail(self):
        body = self.csv()
        cases = [b'', b'<html>Unavailable</html>', body.replace(b'Symbol,', b'Wrong,'),
                 body.replace(b'Unsolicited_SMS', b'GSM'), body + b'NA,TRUNCATED\n', body.splitlines()[0] + b'\n']
        for raw in cases:
            with self.subTest(raw=raw[:30]), self.assertRaises((Block, ValueError)):
                ex.surveillance(raw, self.descriptor(), self.now)

    def test_nse_ban_explicit_nil_and_date(self):
        report = ex.bans(b'Securities in Ban For Trade Date 16-NOV-2026: NIL\n', self.now)
        self.assertTrue(report['explicit_nil']); self.assertFalse(report['applies_to_retrieval_date'])
        self.assertEqual(report['trade_date'], '2026-11-16')

    def test_nonempty_ban_and_strict_rows(self):
        head = b'Securities in Ban For Trade Date 15-NOV-2026:\n'
        report = ex.bans(head + b'Sr.No.,Symbol\n1,DEMO\n2,OTHER\n', self.now)
        self.assertEqual(report['symbols'], ['DEMO', 'OTHER'])
        for suffix in (b'', b'Symbol\n', b'Symbol\nNIL\n', b'Symbol\nDEMO\nDEMO\n', b'Sr.No.,Symbol\n2,DEMO\n'):
            with self.subTest(suffix=suffix), self.assertRaises(Block): ex.bans(head + suffix, self.now)

    def test_bad_ban_date_html_and_nil_with_rows_fail(self):
        for body in (b' \n', b'<html>No report</html>', b'Securities in Ban For Trade Date 01-NOV-2026: NIL',
                     b'Securities in Ban For Trade Date 30-NOV-2026: NIL',
                     b'Securities in Ban For Trade Date 15-NOV-2026: NIL\nDEMO'):
            with self.subTest(body=body), self.assertRaises(Block): ex.bans(body, self.now)

    def test_collection_does_not_treat_bse_absence_or_next_day_as_clear(self):
        def fetch(url):
            if url == ex.CATALOGUE: return json.dumps(self.catalogue()).encode()
            if url == ex.BAN: return b'Securities in Ban For Trade Date 16-NOV-2026: NIL'
            return self.csv({'GSM': '0'})
        report = ex.collect(['DEMO.NS', '123456.BO', 'UNKNOWN.NS'], fetcher=fetch, clock=lambda: self.now)
        self.assertEqual(report['symbols']['DEMO.NS']['surveillance_status'], 'OBSERVED')
        self.assertEqual(report['symbols']['123456.BO']['surveillance_status'], 'UNKNOWN')
        self.assertEqual(report['symbols']['UNKNOWN.NS']['surveillance_status'], 'UNKNOWN')
        self.assertEqual(report['symbols']['DEMO.NS']['ban_status'], 'UNKNOWN')
        self.assertFalse(report['execution_eligible']); self.assertFalse(report['full_market_coverage'])

    def test_failures_are_independent_no_substitute(self):
        def fetch(url):
            if url == ex.BAN: return b'Securities in Ban For Trade Date 15-NOV-2026: NIL'
            raise OSError('failure')
        report = ex.collect(['DEMO.NS'], fetcher=fetch, clock=lambda: self.now)
        self.assertEqual([r['status'] for r in report['coverage']], ['FAILED', 'COLLECTED'])
        self.assertEqual(report['symbols']['DEMO.NS']['ban_status'], 'NOT_LISTED')
        self.assertFalse(report['execution_eligible'])

    def test_url_allowlist_precedes_network(self):
        with patch.object(ex.urllib.request, 'build_opener') as opener:
            for url in ('https://127.0.0.1/', ex.BASES[0] + '../REG_IND131126.csv', ex.BAN + '?token=secret',
                        'https://nsearchives.nseindia.in.evil/content/cm/REG_IND131126.csv'):
                with self.assertRaises(Block): ex.fetch(url)
            opener.assert_not_called()
        self.assertTrue(ex.allowed_url(self.descriptor()['url']))

    def test_combined_collector_includes_exchange_failures(self):
        from india_collect import collect
        report = collect(['DEMO.NS'], fetcher=Mock(side_effect=OSError('offline')),
                         charts=Mock(return_value={}), news=Mock(return_value={'sources': [], 'records': []}),
                         filings=Mock(return_value={'filings': [], 'attempted': 0, 'deferred': 0}), clock=lambda: self.now)
        self.assertIn('exchange_observations', report)
        self.assertTrue(any(r['component'] == 'nse_surveillance' and r['status'] == 'FAILED' for r in report['coverage']))

    def test_observed_close_spelling_preserves_source_and_age_checks(self):
        from india_collect import status_observation
        raw = json.dumps({'marketState': [{'market': 'Capital Market', 'marketStatus': 'Close',
                                          'tradeDate': '15-Nov-2026 09:59'}]}).encode()
        report = status_observation(raw, self.now)
        self.assertEqual(report['market_status'], 'Closed'); self.assertEqual(report['source_market_status'], 'Close')
        with self.assertRaises(Block): status_observation(raw, instant('2026-11-16T10:00:00+05:30'))


if __name__ == '__main__':
    unittest.main()

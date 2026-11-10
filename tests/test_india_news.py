import sys
from pathlib import Path
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from india_news import collect,markdown,fetch
from core import instant,Block
NOW=instant('2026-11-10T10:00:00+05:30')
BODY=b'<rss><channel><item><title>Company profit rises</title><link>https://example.com/story</link><pubDate>Tue, 10 Nov 2026 03:00:00 GMT</pubDate></item></channel></rss>'
class NewsTests(unittest.TestCase):
    def test_collection_and_dedup(self):
        r=collect(['nse-announcements','et-markets'],fetcher=lambda u:BODY,clock=lambda:NOW)
        self.assertEqual(len(r['records']),1);self.assertEqual(len(r['records'][0]['sources']),2);self.assertIn('earnings',r['records'][0]['categories'])
    def test_partial_failure(self):
        def get(u):
            if 'economictimes' in u:raise OSError()
            return BODY
        self.assertEqual(collect(['nse-announcements','et-markets'],fetcher=get,clock=lambda:NOW)['status'],'PARTIAL')
    def test_all_failed(self):
        self.assertEqual(collect(['nse-actions'],fetcher=lambda u:b'bad',clock=lambda:NOW)['status'],'BLOCKED')
    def test_empty_feed_not_clearance(self):
        r=collect(['nse-actions'],fetcher=lambda u:b'<rss><channel/></rss>',clock=lambda:NOW)
        self.assertIn('does not mean no market news',markdown(r))
    def test_future_item_visible_as_unparsed(self):
        r=collect(['nse-actions'],fetcher=lambda u:BODY.replace(b'10 Nov',b'11 Nov'),clock=lambda:NOW)
        self.assertEqual(r['status'],'PARTIAL');self.assertEqual(r['records'],[])
    def test_markdown_has_links_times_and_escapes(self):
        r=collect(['nse-actions'],fetcher=lambda u:BODY,clock=lambda:NOW)
        r['records'][0]['headline']='<script>[click](evil)'
        text=markdown(r);self.assertNotIn('<script>',text);self.assertIn('https://example.com/story',text);self.assertIn('+05:30',text)
    def test_source_allowlist(self):
        with self.assertRaises(Block):fetch('https://evil.example')
    def test_invalid_window(self):
        with self.assertRaises(Block):collect(['nse-actions'],721)
    def test_nse_local_time_is_explicitly_assumed(self):
        body=BODY.replace(b'Tue, 10 Nov 2026 03:00:00 GMT',b'10-Nov-2026 08:30:00')
        r=collect(['nse-actions'],fetcher=lambda u:body,clock=lambda:NOW)
        self.assertEqual(len(r['records']),1);self.assertEqual(r['status'],'PARTIAL')
        self.assertFalse(r['records'][0]['timezone_verified']);self.assertIn('IST assumed',markdown(r))
    def test_unrelated_source_never_gets_nse_timezone(self):
        body=BODY.replace(b'Tue, 10 Nov 2026 03:00:00 GMT',b'10-Nov-2026 08:30:00')
        r=collect(['et-markets'],fetcher=lambda u:body,clock=lambda:NOW)
        self.assertEqual(r['records'],[])
    def test_default_rss_remains_strict_for_risk_consumers(self):
        from data import rss,SOURCES
        body=BODY.replace(b'Tue, 10 Nov 2026 03:00:00 GMT',b'10-Nov-2026 08:30:00')
        self.assertEqual(rss(body,SOURCES['corporate_actions'],NOW)['records'],[])
    def test_future_nse_local_time_stays_rejected(self):
        body=BODY.replace(b'Tue, 10 Nov 2026 03:00:00 GMT',b'11-Nov-2026 08:30:00')
        self.assertEqual(collect(['nse-actions'],fetcher=lambda u:body,clock=lambda:NOW)['records'],[])
    def test_sast_minute_precision_is_preserved(self):
        body=BODY.replace(b'Tue, 10 Nov 2026 03:00:00 GMT',b'10-Nov-2026 08:30')
        r=collect(['nse-sast'],fetcher=lambda u:body,clock=lambda:NOW)
        self.assertEqual(r['records'][0]['publication_time_raw'],'10-Nov-2026 08:30')
        self.assertFalse(r['records'][0]['timezone_verified'])

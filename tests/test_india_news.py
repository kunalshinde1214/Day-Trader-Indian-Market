import sys
from pathlib import Path
import unittest
from unittest.mock import patch
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


DIGEST=('<rss><channel>'
  '<item><title>Alpha Ltd</title><link>https://example.com/a</link><pubDate>Tue, 10 Nov 2026 03:00:00 GMT</pubDate>'
  '<description>Alpha Ltd has informed the Exchange about Copy of Newspaper Publication |SUBJECT: Copy of Newspaper Publication</description></item>'
  '<item><title>Beta Ltd</title><link>https://example.com/b</link><pubDate>Tue, 10 Nov 2026 02:00:00 GMT</pubDate>'
  '<description>Beta Ltd has informed the Exchange about receipt of a 500 MW order |SUBJECT: Bagging/Receiving of orders/contracts</description></item>'
  '<item><title>Gamma Ltd</title><link>https://example.com/c</link><pubDate>Tue, 10 Nov 2026 04:00:00 GMT</pubDate>'
  '<description>Gamma Ltd has informed the Exchange about General Updates |SUBJECT: General Updates</description></item>'
  '<item><title>Delta ETF</title><link>https://example.com/d</link><pubDate>Tue, 10 Nov 2026 04:30:00 GMT</pubDate>'
  '<description>NAV |SUBJECT: Declaration of NAV</description></item>'
  '</channel></rss>').encode()


class DigestTests(unittest.TestCase):
    def report(self):
        return collect(['nse-announcements'],fetcher=lambda u:DIGEST,clock=lambda:NOW)

    def test_subjects_are_ranked(self):
        levels={r['headline']:r['importance'] for r in self.report()['records']}
        self.assertEqual(levels,{'Alpha Ltd':'routine','Beta Ltd':'material','Gamma Ltd':'notable','Delta ETF':'routine'})

    def test_digest_hides_routine_and_leads_with_material(self):
        text=markdown(self.report())
        self.assertNotIn('Alpha Ltd',text);self.assertNotIn('Delta ETF',text)
        self.assertIn('routine filings hidden: 2',text)
        self.assertLess(text.index('Beta Ltd'),text.index('Gamma Ltd'))  # material before newer notable item
        self.assertIn('Beta Ltd — Bagging/Receiving of orders/contracts',text)
        self.assertIn('receipt of a 500 MW order',text)

    def test_include_routine_shows_everything(self):
        text=markdown(self.report(),include_routine=True)
        for name in ('Alpha Ltd','Beta Ltd','Gamma Ltd','Delta ETF'):self.assertIn(name,text)

    def test_items_without_subject_stay_visible(self):
        r=collect(['et-markets'],fetcher=lambda u:BODY,clock=lambda:NOW)
        self.assertEqual(r['records'][0]['importance'],'notable');self.assertIn('Company profit rises',markdown(r))


ACTIONS=('<rss><channel>'
  '<item><title>Oil India Limited - Ex-Date: 20-Nov-2026 </title><link>https://www.nseindia.com/companies-listing/corporate-filings-actions</link>'
  '<description>SERIES:EQ |PURPOSE:INTERIM DIVIDEND - RS 7 PER SHARE |FACE VALUE:10 |RECORD DATE:20-Nov-2026</description><pubDate>10-Nov-2026 06:11:45</pubDate></item>'
  '<item><title>Tiny Cotspin Limited - Ex-Date: 23-Nov-2026 </title><link>https://www.nseindia.com/companies-listing/corporate-filings-actions</link>'
  '<description>SERIES:EQ |PURPOSE:INTERIM DIVIDEND - RE 0.02 PER SHARE |FACE VALUE:1 |RECORD DATE:23-Nov-2026</description><pubDate>10-Nov-2026 06:11:45</pubDate></item>'
  '<item><title>Small Buyback Limited - Ex-Date: 18-Nov-2026 </title><link>https://www.nseindia.com/companies-listing/corporate-filings-actions</link>'
  '<description>SERIES:EQ |PURPOSE:BUY BACK |FACE VALUE:2 |RECORD DATE:18-Nov-2026</description><pubDate>10-Nov-2026 06:11:45</pubDate></item>'
  '</channel></rss>').encode()
NIFTY100=b'Company Name,Industry,Symbol,Series,ISIN Code\n'+b''.join(f'Large Company {k} Ltd.,X,LC{k},EQ,INE{k:03d}\n'.encode() for k in range(99))+b'Oil India Ltd.,Oil,OIL,EQ,INE274J01014\n'


class DividendDemotionTests(unittest.TestCase):
    def report(self,large=None):
        from india_news import large_cap_names
        return collect(['nse-actions'],fetcher=lambda u:ACTIONS,clock=lambda:NOW,large_caps=large_cap_names(NIFTY100) if large else None)

    def test_small_company_dividend_ex_dates_are_routine_not_material(self):
        levels={r['headline'].split(' - ')[0]:r['importance'] for r in self.report(large=True)['records']}
        self.assertEqual(levels,{'Oil India Limited':'notable','Tiny Cotspin Limited':'routine','Small Buyback Limited':'material'})
        text=markdown(self.report(large=True))
        self.assertNotIn('Tiny Cotspin',text);self.assertLess(text.index('Small Buyback'),text.index('Oil India'))

    def test_without_a_large_cap_list_dividends_are_never_material(self):
        levels={r['headline'].split(' - ')[0]:r['importance'] for r in self.report()['records']}
        self.assertEqual(levels['Tiny Cotspin Limited'],'notable');self.assertEqual(levels['Small Buyback Limited'],'material')

    def test_large_cap_list_url_is_allowlisted_and_name_matching_ignores_suffixes(self):
        from india_news import LARGE_CAP_LIST,company_key
        self.assertEqual(company_key('Oil India Limited - Ex-Date: 20-Nov-2026 '),company_key('Oil India Ltd.'))
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=OSError('offline')
            with self.assertRaises(OSError):fetch(LARGE_CAP_LIST)


class GoogleNewsTests(unittest.TestCase):
    """Google News search RSS (India edition): one exact URL shape, publisher split from the title, newest first."""
    def test_query_urls_are_exact_and_encoded(self):
        import india_news
        url = india_news.google_news_url('"Titan Company" share when:1d')
        self.assertEqual(url, "https://news.google.com/rss/search?q=%22Titan+Company%22+share+when%3A1d&hl=en-IN&gl=IN&ceid=IN:en")
        self.assertEqual(india_news.FEEDS["google-news-markets"], "https://news.google.com/rss/search?q=Sensex+Nifty+when%3A1d&hl=en-IN&gl=IN&ceid=IN:en")
        for bad in ("https://news.google.com/rss/search?q=x&hl=en-US&gl=US&ceid=US:en", "https://news.google.com/rss/articles/abc",
                    "http://news.google.com/rss/search?q=x&hl=en-IN&gl=IN&ceid=IN:en", "https://news.google.com.evil/rss/search?q=x&hl=en-IN&gl=IN&ceid=IN:en"):
            with self.assertRaises(Block): fetch(bad)
        with self.assertRaises(Block): india_news.google_news_url("x" * 300)

    def test_search_splits_publisher_and_keeps_recent_items_newest_first(self):
        import india_news
        body = (b'<rss><channel>'
                b'<item><title>Old story - Mint</title><link>https://news.google.com/rss/articles/o</link><pubDate>Fri, 06 Nov 2026 03:00:00 GMT</pubDate></item>'
                b'<item><title>Titan slides as gold falls - Upstox</title><link>https://news.google.com/rss/articles/a</link><pubDate>Tue, 10 Nov 2026 03:00:00 GMT</pubDate></item>'
                b'<item><title>Titan Q2: margins - what next - Business Standard</title><link>https://news.google.com/rss/articles/b</link><pubDate>Tue, 10 Nov 2026 04:00:00 GMT</pubDate></item>'
                b'</channel></rss>')
        rows = india_news.search('"Titan Company" share when:1d', 24, fetcher=lambda url: body, clock=lambda: NOW)
        self.assertEqual([(r["headline"], r["publisher"]) for r in rows],
                         [("Titan Q2: margins - what next", "Business Standard"), ("Titan slides as gold falls", "Upstox")])
        self.assertEqual(rows[0]["published_at"], "2026-11-10T09:30:00+05:30")

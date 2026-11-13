"""Synthetic one-minute and custom-level workflows, never broker acceptance."""
import copy
from datetime import timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from core import Block, instant, setup_hit, plan_from_hit, size, ticket
from demo import inputs
from replay import replay
from entry_policy import check, check_candles, level_reference, interval


def minute_bars(rows):
    # Explicit fictional expansion; never applied to real data in application code.
    return [{**b,'at':(instant(b['at'])+timedelta(minutes=k)).isoformat(),'volume':b['volume']/5}
            for b in rows for k in range(5)]


class EntryIntervalTests(unittest.TestCase):
    def fixture(self,broker='kite',setup='orb',minutes=1):
        c=json.loads((ROOT/'assets/config.example.json').read_text())
        c.update(broker=broker,setup=setup,interval_minutes=minutes,replay_paths=20)
        card=json.loads((ROOT/'assets/charges.example.json').read_text());card['broker']=broker
        now,i,ctx,h,u,b=inputs(c)
        rows=b['instruments']['NSE:DEMO-EQ']['bars']
        if minutes==1:
            rows=minute_bars(rows);h['bars']=minute_bars(h['bars']);h['interval']='minute'
            # Keep the breakout at 09:34 rather than introducing an earlier fictional hit.
            for row in rows[15:19]:row.update(open=100.5,high=100.9,low=100.4,close=100.5)
        c['level_entry']={**{k:i[k] for k in ('broker','exchange','symbol','token')},'side':'BUY','price':101,
                          'for_session':now.date().isoformat(),'available_at':now.replace(minute=15).isoformat(),'source':'synthetic supplied level'}
        return c,card,now,i,ctx,h,rows

    def test_one_minute_orb_and_level_paper_tickets_all_brokers(self):
        for broker in ('kite','upstox','dhan','angel','fyers'):
            for setup in ('orb','level'):
                with self.subTest(broker=broker,setup=setup):
                    c,card,now,i,ctx,h,rows=self.fixture(broker,setup)
                    hit=setup_hit(rows,now,c,i);p=plan_from_hit(hit,i,c,now,rows=rows)
                    self.assertEqual(p['interval_minutes'],1)
                    ctx['replay']=replay(p,i,h,c,card)
                    result=ticket(size(p,i,c,ctx,card,now),i,c,ctx,card,now)
                    self.assertEqual(result['risk']['status'],'PASS');self.assertFalse(result['submitted'])

    def test_five_minute_custom_level(self):
        c,card,now,i,ctx,h,rows=self.fixture(setup='level',minutes=5)
        p=plan_from_hit(setup_hit(rows,now,c,i),i,c,now)
        self.assertEqual(p['as_of'],now.isoformat())
        r=replay(p,i,h,c,card)
        self.assertEqual(r['historical_level_reference_sessions'],0)
        self.assertEqual(r['historical_same_side_firings'],0)
        h['level_entries']=[]
        with self.assertRaisesRegex(Block,'LEVEL'):replay(p,i,h,c,card)

    def test_history_interval_metadata_cannot_be_relabelled(self):
        c,card,now,i,ctx,h,rows=self.fixture()
        p=plan_from_hit(setup_hit(rows,now,c,i),i,c,now)
        h['interval']='5minute'
        with self.assertRaisesRegex(Block,'INTERVAL'):replay(p,i,h,c,card)

    def test_gaps_and_forming_bar_cannot_trigger(self):
        c,card,now,i,ctx,h,rows=self.fixture()
        with self.assertRaisesRegex(Block,'BARS'):setup_hit(rows[:6]+rows[7:],now,c,i)
        self.assertEqual(setup_hit(rows,now-timedelta(seconds=1),c,i)['state'],'waiting')

    def test_level_availability_blocks_lookahead_and_foreign_identity(self):
        c,card,now,i,ctx,h,rows=self.fixture(setup='level')
        for changes in ({'token':'foreign'},{'for_session':'2026-10-29'},{'price':101.001},
                        {'available_at':(now+timedelta(seconds=1)).isoformat()},{'source':''}):
            with self.subTest(changes=changes),self.assertRaises(Block):
                level_reference({**c['level_entry'],**changes},i,now)
        c['level_entry']['available_at']=(now-timedelta(seconds=30)).isoformat()
        self.assertEqual(setup_hit(rows,now,c,i)['state'],'waiting')

    def test_level_and_interval_changes_invalidate_plan(self):
        c,card,now,i,ctx,h,rows=self.fixture(setup='level')
        p=plan_from_hit(setup_hit(rows,now,c,i),i,c,now)
        with self.assertRaises(Block):check(p,{**c,'interval_minutes':5})
        with self.assertRaises(Block):check(p,{**c,'level_entry':{**c['level_entry'],'price':100}})

    def test_invalid_intervals_and_unsupported_rules(self):
        for minutes in (True,0,2,'1'):
            with self.assertRaises(Block):interval({'interval_minutes':minutes})
        with self.assertRaises(Block):interval({'interval_minutes':1,'setup':'gap_and_go'})
        with self.assertRaises(Block):check_candles({}, {'interval_minutes':1})

    def test_one_minute_adapter_wire_contracts(self):
        from kite_readonly import KiteReadOnly
        from upstox_readonly import UpstoxReadOnly
        from dhan_readonly import DhanReadOnly
        from angel_readonly import AngelReadOnly
        from fyers_readonly import FyersReadOnly
        start='2026-10-30T09:15:00+05:30';end='2026-10-30T09:17:00+05:30'
        candle=['2026-10-30T09:16:00+05:30',100,101,99,100,10]
        cases=[(KiteReadOnly(),'999','NSE',{'candles':[candle]},{}),
               (UpstoxReadOnly(),'NSE_EQ|DEMO00000001','NSE',{'candles':[candle]},{}),
               (DhanReadOnly(),'999','NSE',{'timestamp':[int(instant(candle[0]).timestamp())],**{k:[v] for k,v in zip(('open','high','low','close','volume'),candle[1:])}},{'instrument_type':'EQUITY'}),
               (AngelReadOnly(),'999','NSE',[candle],{}),
               (FyersReadOnly(),'10100000999999','NSE',{'candles':[[int(instant(candle[0]).timestamp()),*candle[1:]]]}, {})]
        for client,token,exchange,data,kwargs in cases:
            with self.subTest(client=type(client).__name__),patch.object(client,'request',return_value=data) as request:
                bounds=('2026-10-30','2026-10-30') if isinstance(client,UpstoxReadOnly) else (start,end)
                out=client.candles(token,'DEMO-EQ',exchange,*bounds,'minute',**kwargs)
                self.assertEqual(out['interval'],'minute');self.assertEqual(len(out['bars']),1)
                args=request.call_args
                if isinstance(client,KiteReadOnly):self.assertIn('/minute',args.args[1])
                elif isinstance(client,UpstoxReadOnly):self.assertIn('/minutes/1/',args.args[1])
                elif isinstance(client,DhanReadOnly):self.assertEqual(args.args[2]['interval'],'1')
                elif isinstance(client,AngelReadOnly):self.assertEqual(args.args[2]['interval'],'ONE_MINUTE')
                else:self.assertEqual(args.kwargs['query']['resolution'],'1')

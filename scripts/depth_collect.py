"""Read-only Kite cash-equity depth and observed trailing traded-unit collection."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time

from core import Block, IST, fresh, instant, integer, positive, on_tick, validate_instrument
from broker_data import normalize_quote, quote_key


def observation(result, instrument, now):
    validate_instrument(instrument, now)
    if instrument['broker'] != 'kite' or instrument['kind'] != 'equity':
        raise Block('DEPTH: Kite cash equities only; derivative volume units need acceptance')
    normalize_quote(result, instrument, now, 30)
    row = result['data'][quote_key(instrument)]
    stamp = datetime.fromisoformat(row['timestamp'])
    if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=IST)
    stamp = stamp.astimezone(IST)
    fresh(stamp.isoformat(), now, 30, 'exchange depth timestamp')
    if stamp.date() != now.date(): raise Block('DEPTH: same-session depth required')
    ladders = {}
    for target, source in (('bids', 'buy'), ('asks', 'sell')):
        raw = row['depth'][source]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 5: raise Block('DEPTH: bounded five-level book required')
        rows = []
        for level in raw:
            quantity = integer(level['quantity'], minimum=0)
            if not quantity: continue
            price = positive(level['price'])
            if not on_tick(price, instrument['tick_size']): raise Block('DEPTH: off-tick price')
            rows.append({'price': float(price), 'quantity': quantity})
        prices = [r['price'] for r in rows]
        if not rows or prices != sorted(set(prices), reverse=target == 'bids'): raise Block('DEPTH: empty/unsorted/duplicate prices')
        ladders[target] = rows
    if ladders['bids'][0]['price'] >= ladders['asks'][0]['price']: raise Block('DEPTH: crossed book')
    return {**{k: instrument[k] for k in ('broker','exchange','symbol','token')}, 'source':'kite',
            'as_of':stamp.isoformat(), 'quantity_unit':'units', 'cumulative_volume':integer(row['volume'], minimum=0), **ladders}


def combine(first, second):
    if any(first[k] != second[k] for k in ('broker','exchange','symbol','token')):
        raise Block('DEPTH: observations differ in identity')
    start, end = instant(first['as_of']), instant(second['as_of'])
    if start.date() != end.date() or not 1 <= (end-start).total_seconds() <= 300:
        raise Block('DEPTH: observation window must be 1..300 seconds in one session')
    traded = second['cumulative_volume'] - first['cumulative_volume']
    if traded < 0: raise Block('DEPTH: cumulative volume reset/regressed')
    return {k:v for k,v in second.items() if k != 'cumulative_volume'} | {
        'volume_window_start':start.isoformat(), 'volume_window_end':end.isoformat(), 'traded_units':traded,
        'coverage':'Two observed cumulative-volume samples; displayed depth can disappear before execution.'}


def collect(client, instrument, *, interval=2, clock=instant, sleep=time.sleep):
    interval = integer(interval)
    if interval > 60: raise Block('DEPTH: sampling interval must be 1..60 seconds')
    first = observation(client.quotes([quote_key(instrument)]), instrument, clock())
    sleep(interval)
    second = observation(client.quotes([quote_key(instrument)]), instrument, clock())
    return combine(first, second)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instrument',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--interval',type=int,default=2);args=parser.parse_args(argv)
    try:
        from trader import read
        from kite_readonly import KiteReadOnly
        path=Path(args.out)
        if path.exists():raise Block('OUTPUT: preserve previous observation')
        report=collect(KiteReadOnly(),read(args.instrument),interval=args.interval)
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:json.dump(report,stream,indent=2,allow_nan=False)
        print(json.dumps({'status':'SAVED','submitted':False,'path':str(path)}));return 0
    except (Block,KeyError,ValueError,TypeError,OSError):
        print('BLOCKED: depth identity, timing, units or book validation failed');return 2


if __name__=='__main__':raise SystemExit(main())

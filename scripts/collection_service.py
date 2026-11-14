"""Bounded read-only public collection with durable per-cycle evidence. Never trades."""
import argparse
import json
from pathlib import Path
import time
import uuid

from core import Block, instant, integer
from india_collect import collect
from service import ProcessLock
from trader import read, write


def run(directory, settings, *, cycles=1, interval=300, collector=collect, clock=instant, sleep=time.sleep):
    directory=Path(directory);cycles=integer(cycles);interval=integer(interval)
    if cycles>288 or not 60<=interval<=3600:raise Block('COLLECTION: 1..288 cycles and 60..3600 second spacing required')
    if settings.get('schema')!='public_collection_settings_v1':raise Block('COLLECTION: explicit public settings required')
    allowed={'schema','symbols','sources','hours','filing_limit','universe'}
    if set(settings)-allowed:raise Block('COLLECTION: unknown settings; credentials do not belong here')
    completed=0;failures=0;reason='cycle_limit';reports=[]
    with ProcessLock(directory/'collection.lock'):
        try:
            for _ in range(cycles):
                if (directory/'stop.json').exists():reason='requested_stop';break
                now=clock();name=now.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex+'.json'
                try:
                    result=collector(settings['symbols'],sources=settings.get('sources'),hours=settings.get('hours',24),
                                     filing_limit=settings.get('filing_limit',5),universe=settings.get('universe'),
                                     archive=directory/'filings.sqlite',clock=clock)
                except (Block,ValueError,KeyError,TypeError,OSError):
                    failures+=1
                    result={'schema':'india_collection_failure_v1','status':'FAILED','as_of':now.isoformat(),
                            'full_market_coverage':False,'execution_eligible':False,'submitted':False,
                            'reason':'Collection failed; inspect configured sources. Private response content omitted.'}
                # Immutable per-cycle report precedes the latest pointer; a crash never fabricates completion.
                directory.mkdir(parents=True,exist_ok=True)
                with (directory/name).open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2,allow_nan=False)
                completed+=1;reports.append(name)
                write(directory/'latest.json',{'report':name,'status':result['status'],'as_of':clock().isoformat(),
                                               'full_market_coverage':False,'execution_eligible':False})
                if completed<cycles:sleep(interval)
        except BaseException:
            reason='interrupted_or_failed';raise
        finally:
            write(directory/'health.json',{'state':'stopped','reason':reason,'completed_cycles':completed,
                  'failed_cycles':failures,'as_of':clock().isoformat(),'submitted':False,'full_market_coverage':False})
    return {'completed_cycles':completed,'failed_cycles':failures,'reason':reason,'reports':reports,'submitted':False}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--settings',required=True);p.add_argument('--directory',required=True)
    p.add_argument('--cycles',type=int,default=1);p.add_argument('--interval',type=int,default=300);a=p.parse_args(argv)
    try:
        result=run(a.directory,read(a.settings),cycles=a.cycles,interval=a.interval)
        print(json.dumps(result,indent=2));return 2 if result['failed_cycles'] else 0
    except (Block,ValueError,KeyError,TypeError,OSError,KeyboardInterrupt):
        print('BLOCKED: invalid settings or interrupted collection; inspect saved health and reports');return 2


if __name__=='__main__':raise SystemExit(main())

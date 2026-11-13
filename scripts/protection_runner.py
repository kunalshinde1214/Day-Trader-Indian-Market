"""Opt-in foreground protection runner. Human arm required; no entries or service installation."""
import argparse
import json
from pathlib import Path
import sqlite3
import time

from core import Block, fingerprint, fresh, instant, integer
from execution import ExecutionStore, TerminalApproval, execute, profile_check
from live import load_bundle, snapshot
from broker import adapter
from broker_data import normalize_quote, quote_key
from live_broker import LiveBroker
from protection import preview
from service import ProcessLock
from trader import read, write


def scope(workdir):
    w=Path(workdir)
    values={name:read(w/(name+'.json')) for name in ('config','instrument','live-profile','protection-policy')}
    return fingerprint(values),values


def eligibility(values, now):
    c,p=values['config'],values['live-profile']
    profile_check(p,c['broker'],c['account_id'],now)
    evidence=p.get('automation_acceptance',{})
    if (p.get('unattended_protection') is not True or evidence.get('status')!='PASS'
            or any(evidence.get(k)!=c[k] for k in ('broker','account_id'))
            or not evidence.get('source') or evidence.get('broker_api_automation_permitted') is not True):
        raise Block('AUTOMATION: explicit opt-in and account-specific broker automation acceptance required')
    fresh(evidence.get('as_of'),now,86400,'automation acceptance')
    return c


def validate_mandate(mandate, scope_hash, values, records, now, revoked=False):
    c=eligibility(values,now)
    if (revoked or mandate.get('schema')!='protection_mandate_v1' or mandate.get('scope_hash')!=scope_hash
            or any(mandate.get(k)!=c[k] for k in ('broker','account_id'))):
        raise Block('MANDATE: revoked or changed configuration')
    start,end=instant(mandate['armed_at']),instant(mandate['expires_at'])
    if not start<=now<end or start.date()!=now.date() or end.date()!=now.date() or (end-start).total_seconds()>28800:
        raise Block('MANDATE: expired, future or invalid session window')
    limit=integer(mandate['max_actions'])
    if limit>20:raise Block('MANDATE: maximum twenty actions per arming')
    digest=fingerprint(mandate)
    own=[r for r in records if r['review'].get('mandate_hash')==digest]
    if len(own)>=limit:raise Block('MANDATE: action budget exhausted')
    if any(r['state'] in ('REJECTED','UNKNOWN','SUBMITTING') for r in own):
        raise Block('MANDATE: failed or ambiguous submission; human recovery required')
    return digest


def observe(w,client,store,now):
    b=load_bundle(w);i=b['instrument']
    b['context']['quote']=normalize_quote(client.quotes([quote_key(i)]),i,now,30)
    snap=snapshot(client,w,b)
    records=store.observe(snap,now)
    decision=preview(w/'state/protection.sqlite',b,snap,read(w/'live-profile.json'),read(w/'protection-policy.json'),now,records)
    return decision,snap


class MandateApproval:
    """Use a previously human-armed local mandate, never per-order fake confirmation."""
    def __init__(self, check):self.check=check
    def approve(self, review):
        digest=self.check()
        if review.get('mandate_hash')!=digest or review['action']['kind'] not in ('stop','exit','cancel'):
            raise Block('MANDATE: action outside standing reducing authorization')
        return review['review_hash']


def arm(w, expires_at, max_actions, *, approval=None, clock=instant):
    w=Path(w);now=clock();digest,values=scope(w);c=eligibility(values,now)
    mandate={'schema':'protection_mandate_v1','scope_hash':digest,'broker':c['broker'],'account_id':c['account_id'],
             'armed_at':now.isoformat(),'expires_at':instant(expires_at).isoformat(),'max_actions':integer(max_actions),
             'policy_id':values['protection-policy']['policy_id']}
    validate_mandate(mandate,digest,values,[],now)
    store=ExecutionStore(w/'state/execution.sqlite')
    try:
        client=adapter(c['broker']);decision,snap=observe(w,client,store,clock())
        if decision['next_state']['phase']=='COMPLETE':raise Block('MANDATE: selected lifecycle is already complete')
        review={'action':{'kind':'arm_protection','broker':c['broker'],'account_id':c['account_id']},
                'mandate':mandate,'policy':values['protection-policy'],'state_hash':snap['state_hash'],
                'warning':'Arms unattended stop/cancel/reducing-limit actions until expiry or action cap. No entry orders. Flatness and fills are not guaranteed.'}
        review['review_hash']=fingerprint(review)
        if (approval or TerminalApproval()).approve(review)!=review['review_hash']:raise Block('MANDATE: human arming declined')
        fresh(now.isoformat(),clock(),90,'mandate approval')
        new_digest,new_values=scope(w)
        validate_mandate(mandate,new_digest,new_values,store.records(c['broker'],c['account_id']),clock())
        _,current=observe(w,client,store,clock())
        if current['state_hash']!=snap['state_hash']:raise Block('MANDATE: account changed during arming; review again')
        if (w/'state/protection-revoked.json').exists():raise Block('MANDATE: revocation latch present; inspect before explicitly removing it')
        write(w/'state/protection-mandate.json',mandate)
        return {'status':'ARMED','expires_at':mandate['expires_at'],'max_actions':mandate['max_actions'],'submitted':False}
    finally:store.close()


def run(w, *, cycles=12, interval=5, clock=instant, sleep=time.sleep):
    w=Path(w);cycles=integer(cycles);interval=integer(interval)
    if cycles>8640 or not 5<=interval<=60:raise Block('AUTOMATION: bounded cycles and 5..60 second interval required')
    mandate=read(w/'state/protection-mandate.json');scope_hash,values=scope(w);c=values['config']
    store=ExecutionStore(w/'state/execution.sqlite');submitted=0;status='CYCLE_LIMIT'
    def check():
        current_hash,current_values=scope(w)
        if read(w/'state/protection-mandate.json')!=mandate:raise Block('MANDATE: arming changed while running')
        return validate_mandate(mandate,current_hash,current_values,store.records(c['broker'],c['account_id']),clock(),
                                (w/'state/protection-revoked.json').exists())
    try:
        check();client=adapter(c['broker']);transport=LiveBroker(c['broker'],client)
        def refresh():
            digest=check();decision,snap=observe(w,client,store,clock());check()
            if decision['review'] is None:raise Block('REVIEW_CHANGED: no action remains; restart after review')
            review=decision['review']
            review.update(mandate_hash=digest,authorization_mode='human-armed bounded protection mandate')
            review['review_hash']=fingerprint({k:v for k,v in review.items() if k not in ('review_hash','as_of')})
            return review,snap
        for cycle in range(cycles):
            check();decision,_=observe(w,client,store,clock());check()
            status=decision['status']
            if status=='FLAT_OBSERVED':break
            if decision['review'] is not None:
                execute(store,transport,MandateApproval(check),refresh,clock=clock);submitted+=1
            write(w/'state/protection-health.json',{'as_of':clock().isoformat(),'status':status,'submitted_this_run':submitted,'cycle':cycle+1})
            if cycle+1<cycles:sleep(interval)
        return {'status':status,'submitted_this_run':submitted,'unattended_execution':True,'flat_guaranteed':False}
    except BaseException:
        write(w/'state/protection-revoked.json',{'as_of':clock().isoformat(),'reason':'Runner stopped on failure/interruption; reconcile before rearming'})
        raise
    finally:store.close()


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=('arm','run','revoke'))
    p.add_argument('--workdir',required=True);p.add_argument('--expires-at');p.add_argument('--max-actions',type=int,default=6)
    p.add_argument('--cycles',type=int,default=12);p.add_argument('--interval',type=int,default=5);a=p.parse_args(argv);w=Path(a.workdir)
    try:
        if a.command=='revoke':
            write(w/'state/protection-revoked.json',{'as_of':instant().isoformat(),'reason':'User revoked'});result={'status':'REVOKED','submitted':False}
        else:
            with ProcessLock(w/'state/protection-runner.lock'):
                if a.command=='arm':
                    if not a.expires_at:raise Block('MANDATE: explicit expiry required')
                    result=arm(w,a.expires_at,a.max_actions)
                else:result=run(w,cycles=a.cycles,interval=a.interval)
        print(json.dumps(result,indent=2));return 0
    except (Block,ValueError,KeyError,TypeError,OSError,sqlite3.Error,KeyboardInterrupt):
        print('BLOCKED: inspect protection state and shared execution log; never retry an ambiguous request.');return 2


if __name__=='__main__':raise SystemExit(main())

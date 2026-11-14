"""Inventory end-user acceptance artifacts without treating missing evidence as a pass."""
import argparse
import hashlib
import json
from pathlib import Path

from core import Block, BROKERS, instant, fresh
from trader import read

REQUIRED=('broker_readonly','installed_host','production_service','automation_operating')


def assess(spec, root, now):
    if spec.get('schema')!='acceptance_inventory_v1' or spec.get('broker') not in BROKERS or not spec.get('account_id'):
        raise Block('READINESS: exact broker and account inventory required')
    root=Path(root).resolve();rows=[]
    for category in REQUIRED:
        item=spec.get('evidence',{}).get(category)
        if item is None:
            rows.append({'category':category,'status':'MISSING'});continue
        path=(root/item['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():raise Block('READINESS: evidence must be a local file inside inventory directory')
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if digest!=item.get('sha256'):raise Block('READINESS: evidence hash mismatch')
        evidence=read(path)
        identity=all(evidence.get(k)==spec[k] for k in ('broker','account_id'))
        valid=identity and evidence.get('status')=='PASS'
        fresh(evidence.get('as_of'),now,86400,'acceptance evidence')
        if category=='broker_readonly':
            valid=valid and evidence.get('mode')=='authenticated_readonly' and evidence.get('submitted') is False
            valid=valid and evidence.get('checks',{}).get('account_identity_matches') is True
        else:
            valid=valid and evidence.get('category')==category and bool(evidence.get('source'))
            valid=valid and evidence.get('observed') is True and isinstance(evidence.get('checks'),dict) and bool(evidence['checks'])
            valid=valid and all(v is True for v in evidence.get('checks',{}).values())
        rows.append({'category':category,'status':'EVIDENCE_RECORDED' if valid else 'FAILED','sha256':digest})
    return {'schema':'acceptance_readiness_v1','broker':spec['broker'],'account_id':spec['account_id'],'as_of':now.isoformat(),
            'status':'EVIDENCE_RECORDED' if all(r['status']=='EVIDENCE_RECORDED' for r in rows) else 'INCOMPLETE',
            'evidence':rows,'submitted':False,'execution_authorized':False,
            'limitation':'Checks local evidence identity, hashes, declared outcomes and freshness. Does not independently verify operator claims, broker permission, legal compliance or production reliability.'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--out',required=True);a=p.parse_args(argv)
    try:
        path=Path(a.input);out=Path(a.out)
        if out.exists():raise Block('OUTPUT: preserve existing evidence')
        result=assess(read(path),path.parent,instant())
        out.parent.mkdir(parents=True,exist_ok=True)
        with out.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2,allow_nan=False)
        print(json.dumps({'status':result['status'],'execution_authorized':False}));return 0 if result['status']=='EVIDENCE_RECORDED' else 2
    except (Block,ValueError,KeyError,TypeError,OSError):
        print('BLOCKED: acceptance inventory identity, file, hash or freshness check failed');return 2


if __name__=='__main__':raise SystemExit(main())

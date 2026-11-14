"""Generate a disabled local execution profile; no credentials, login or network."""
import argparse
import json
from pathlib import Path
from core import Block, BROKERS


def profile(broker, account):
    if broker not in BROKERS or not isinstance(account,str) or not account.strip() or len(account)>128 or any(c.isspace() for c in account):
        raise Block('SETUP: supported broker and nonsecret account ID required')
    def evidence():return {'broker':broker,'account_id':account,'status':'NOT_TESTED','source':'','as_of':''}
    return {'broker':broker,'account_id':account,'enabled':False,'new_entries':False,
            'approval_mode':'human_terminal_two_stage','max_entry_notional_inr':10000,'max_entry_loss_inr':100,
            'acceptance':evidence(),'mixed_inventory_reducing_exits':False,'mixed_inventory_acceptance':evidence(),
            'unattended_protection':False,'automation_acceptance':{**evidence(),'broker_api_automation_permitted':False}}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--broker',choices=sorted(BROKERS),required=True)
    p.add_argument('--account-id',required=True,help='Nonsecret account ID only; never an API key or token')
    p.add_argument('--out',required=True);a=p.parse_args(argv)
    try:
        result=profile(a.broker,a.account_id);path=Path(a.out);path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:json.dump(result,stream,indent=2,allow_nan=False)
        print(json.dumps({'status':'CREATED_DISABLED','submitted':False,'acceptance':'NOT_TESTED'}));return 0
    except (Block,ValueError,OSError):
        print('BLOCKED: invalid setup or existing output; no profile overwritten');return 2


if __name__=='__main__':raise SystemExit(main())

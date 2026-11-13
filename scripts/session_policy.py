"""Dated, instrument-specific continuous-session evidence; auctions are separate."""
from datetime import time
from urllib.parse import urlsplit
from core import Block, instant, fresh


def validate(evidence, instrument, config, now):
    if not isinstance(evidence,dict):raise Block('SESSION_POLICY: exact instrument session evidence required')
    for key in ('broker','exchange','symbol','token'):
        if str(evidence.get(key))!=str(instrument.get(key)):raise Block('SESSION_POLICY: instrument mismatch')
    if evidence.get('date')!=now.date().isoformat() or evidence.get('account_id')!=config['account_id']:
        raise Block('SESSION_POLICY: date/account mismatch')
    url=urlsplit(evidence.get('source',''))
    if url.scheme!='https' or url.hostname not in ('www.nseindia.com','nsearchives.nseindia.com','archives.nseindia.com','www.bseindia.com','api.bseindia.com') or url.username or url.password:
        raise Block('SESSION_POLICY: official exchange source required')
    fresh(evidence.get('as_of'),now,config['snapshot_max_age_seconds'],'instrument session')
    if evidence.get('is_open_day') is not True or evidence.get('halted') is not False:
        raise Block('SESSION_POLICY: session closed, halted or unknown')
    if now.weekday()>=5 and evidence.get('special_session') is not True:
        raise Block('SESSION_POLICY: explicit special-session evidence required')
    start=time.fromisoformat(evidence['continuous_open']);end=time.fromisoformat(evidence['continuous_close'])
    cutoff=time.fromisoformat(evidence['broker_squareoff'])
    flat=time.fromisoformat(config['flat_by'])
    if not start<=now.time()<end or not start<flat<min(end,cutoff):
        raise Block('SESSION_POLICY: outside continuous window or flat-by exceeds instrument/broker cutoff')
    if evidence.get('auction_included') is not False:
        raise Block('SESSION_POLICY: auction phases cannot authorize continuous-session entries')
    return True

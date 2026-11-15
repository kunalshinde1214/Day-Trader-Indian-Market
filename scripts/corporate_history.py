"""Reconstruct explicitly raw daily equity history for sourced splits/bonuses."""
import argparse
from datetime import date
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from core import Block, instant, integer, positive, number, fingerprint
from inventory_book import evidence_files, decimal_text
from membership_history import HOSTS
from trader import read


def isin(value):
    if not isinstance(value, str) or not re.fullmatch(r'IN[A-Z0-9]{10}', value):
        raise Block('CORPORATE_HISTORY: Indian ISIN required')
    return value


def day(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise Block('CORPORATE_HISTORY: ISO date required')
    return parsed


def reconstruct(spec, root, known_at):
    if spec.get('schema') != 'equity_action_history_v1' or spec.get('reviewed') is not True or spec.get('actions_complete') is not True:
        raise Block('CORPORATE_HISTORY: reviewed complete action window required')
    hashes = evidence_files(spec, root)
    history = spec['history']; target = day(spec['reference_session'])
    if target > known_at.date() or history.get('price_basis') != 'raw_unadjusted':
        raise Block('CORPORATE_HISTORY: nonfuture basis and explicitly raw prices required')
    if history.get('exchange') not in ('NSE', 'BSE') or not isinstance(history.get('symbol'), str) or not re.fullmatch(r'[A-Z0-9&_.-]{1,40}', history['symbol']):
        raise Block('CORPORATE_HISTORY: exact Indian equity required')
    if history.get('evidence_id') not in hashes:
        raise Block('CORPORATE_HISTORY: raw-price source artifact required')
    current_isin = isin(history['initial_isin']); rows = history['bars']; actions = spec['actions']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100000 or not isinstance(actions, list) or len(actions) > 100:
        raise Block('CORPORATE_HISTORY: bounded daily rows/actions required')
    first = day(rows[0]['session']); previous_action = first; seen = set(); normalized = []
    if first > target:
        raise Block('CORPORATE_HISTORY: reversed observation window')
    for action in actions:
        required = {'action_id', 'kind', 'ex_date', 'evidence_id', 'old_isin', 'new_isin', 'numerator', 'denominator'}
        if not isinstance(action, dict) or set(action) != required or action['kind'] not in ('split', 'bonus'):
            raise Block('CORPORATE_HISTORY: exact supported split/bonus fields required')
        aid = action['action_id']; ex_date = day(action['ex_date'])
        if not isinstance(aid, str) or not aid or aid in seen or not previous_action < ex_date <= target:
            raise Block('CORPORATE_HISTORY: unique ordered ex-dates inside the source window required')
        seen.add(aid); previous_action = ex_date
        if action['evidence_id'] not in hashes or isin(action['old_isin']) != current_isin:
            raise Block('CORPORATE_HISTORY: source or ISIN chain mismatch')
        current_isin = isin(action['new_isin'])
        artifact = spec['artifacts'][action['evidence_id']]
        url = urlsplit(artifact['source'])
        if url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password or url.port not in (None, 443):
            raise Block('CORPORATE_HISTORY: official corporate-action source required')
        if instant(artifact['published_at']) > known_at:
            raise Block('CORPORATE_HISTORY: action unavailable at information cutoff')
        numerator, denominator = integer(action['numerator']), integer(action['denominator'])
        if max(numerator, denominator) > 100000:
            raise Block('CORPORATE_HISTORY: unsupported ratio magnitude')
        ratio = number(numerator) / denominator
        if action['kind'] == 'bonus':
            ratio += 1  # numerator additional shares per denominator existing shares.
        if ratio == 1:
            raise Block('CORPORATE_HISTORY: no-op ratio')
        normalized.append({**action, 'share_factor': decimal_text(ratio), 'source': artifact['source'],
                           'published_at': artifact['published_at'], 'sha256': hashes[action['evidence_id']]})
    result = []; previous = None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'session', 'isin', 'open', 'high', 'low', 'close', 'volume', 'available_at'}:
            raise Block('CORPORATE_HISTORY: exact sourced daily-bar fields required')
        session = day(row['session']); available = instant(row['available_at'])
        if session > target or previous is not None and session <= previous or available.date() < session or available > known_at:
            raise Block('CORPORATE_HISTORY: unordered, future or unavailable daily bar')
        previous = session; expected_isin = history['initial_isin']; factor = number(1)
        for action in normalized:
            if session >= day(action['ex_date']): expected_isin = action['new_isin']
            else: factor *= positive(action['share_factor'])
        if isin(row['isin']) != expected_isin or not number('0.000000000001') <= factor <= number('1000000000000'):
            raise Block('CORPORATE_HISTORY: row ISIN mismatch or unsupported cumulative factor')
        values = {k: positive(row[k]) for k in ('open', 'high', 'low', 'close')}
        if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
            raise Block('CORPORATE_HISTORY: contradictory OHLC')
        volume = number(row['volume'])
        result.append({**row, **{k: decimal_text(v / factor) for k, v in values.items()},
                       'volume': decimal_text(volume * factor), 'source_isin': row['isin'], 'isin': current_isin,
                       'share_factor': decimal_text(factor)})
    output = {'schema': 'adjusted_equity_history_v1', 'exchange': history['exchange'], 'symbol': history['symbol'],
              'isin': current_isin, 'reference_session': target.isoformat(), 'known_at': known_at.isoformat(),
              'price_basis': 'split_bonus_adjusted_to_reference_session',
              'volume_basis': 'synthetic_share_equivalent_not_executable_traded_volume', 'bars': result,
              'actions': normalized, 'source_artifact_hashes': hashes, 'input_hash': fingerprint(spec),
              'independently_verified': False, 'execution_eligible': False, 'submitted': False,
              'limitations': ['User-reviewed raw-price basis, action completeness and publication times; hashes do not authenticate claims.',
                              'No dividend total returns, rights, mergers/demergers, fractional entitlements or taxes.',
                              'Not a broker master, liquidity input or live execution history.']}
    output['reconstruction_hash'] = fingerprint(output)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True); parser.add_argument('--known-at', required=True)
    parser.add_argument('--out', required=True); args = parser.parse_args(argv)
    try:
        out = Path(args.out); source = Path(args.input); known_at = instant(args.known_at)
        if out.exists() or known_at > instant():
            raise Block('CORPORATE_HISTORY: new output and nonfuture information cutoff required')
        result = reconstruct(read(source), source.parent, known_at)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream: json.dump(result, stream, indent=2, allow_nan=False)
        print(json.dumps({'status': 'REVIEWED_SOURCE_ONLY', 'rows': len(result['bars']), 'submitted': False}))
        return 0
    except (Block, ValueError, KeyError, TypeError, OSError):
        print('BLOCKED: incomplete or inconsistent sourced corporate-action history')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

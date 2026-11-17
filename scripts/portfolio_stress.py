"""Explicit account-by-account price, collateral and funding scenarios. Research only."""
import argparse
import json
from pathlib import Path
from core import Block, BROKERS, fingerprint, instant, integer, number, positive, money, fresh


def evaluate(spec, now):
    if spec.get('schema') != 'portfolio_stress_v1': raise Block('STRESS: explicit schema required')
    accounts, scenarios = spec['accounts'], spec['scenarios']
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 20 or not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 40:
        raise Block('STRESS: bounded accounts and scenarios required')
    books = {}; names = set()
    for account in accounts:
        key = account['id']
        identity = (account['broker'], account['account_id'])
        if (not isinstance(key, str) or not key or key in books or identity in names or
                identity[0] not in BROKERS or not isinstance(identity[1], str) or not identity[1]):
            raise Block('STRESS: distinct exact account identity required')
        names.add(identity)
        if not account.get('source'): raise Block('STRESS: account evidence source required')
        fresh(account['as_of'], now, 60, 'stress account')
        cash = number(account['cash_balance_inr'], minimum='-1e30')
        required = number(account['account_margin_required_inr'])
        cash_required = number(account['cash_component_required_inr'])
        reserved = number(account['reserved_cash_inr'])
        if cash_required > required: raise Block('STRESS: cash component exceeds total required margin')
        positions = {}; collateral = {}
        for target, rows in ((positions, account['positions']), (collateral, account['collateral'])):
            if not isinstance(rows, list) or len(rows) > 1000: raise Block('STRESS: bounded assets required')
            for row in rows:
                asset = row['id']
                if not isinstance(asset, str) or not asset or asset in target: raise Block('STRESS: distinct asset ID required')
                if row.get('exchange') not in ('NSE', 'BSE', 'NFO', 'BFO') or not row.get('token'):
                    raise Block('STRESS: exact Indian instrument identity required')
                if row.get('price_basis') != 'per_unit_inr': raise Block('STRESS: explicit unit prices required')
                q = integer(row['quantity'], minimum=-1000000000 if target is positions else 1)
                if q == 0: raise Block('STRESS: nonzero units required')
                mark = positive(row['mark_price'])
                haircut = number(row['haircut']) if target is collateral else None
                if haircut is not None and haircut > 1: raise Block('STRESS: haircut must be a fraction')
                target[asset] = (q, mark, haircut)
        books[key] = (account, cash, required, cash_required, reserved, positions, collateral)
    output = []; seen = set()
    for scenario in scenarios:
        name = scenario['name']
        if not isinstance(name, str) or not name or name in seen or set(scenario['accounts']) != set(books):
            raise Block('STRESS: unique scenario with every account explicitly present required')
        seen.add(name); results = []
        for key, (account, cash, required, cash_required, reserved, positions, collateral) in books.items():
            changes = scenario['accounts'][key]
            if set(changes) != {'position_prices', 'collateral_prices', 'collateral_haircuts', 'margin_multiplier', 'cash_component_multiplier', 'cash_debit_inr'}:
                raise Block('STRESS: exact scenario fields required')
            if set(changes['position_prices']) != set(positions) or set(changes['collateral_prices']) != set(collateral) or set(changes['collateral_haircuts']) != set(collateral):
                raise Block('STRESS: every asset needs explicit scenario values; no implicit option repricing')
            pnl = sum((q * (positive(changes['position_prices'][asset]) - mark) for asset, (q, mark, _) in positions.items()), number(0))
            pledged_value = number(0)
            for asset, (q, _, _) in collateral.items():
                haircut = number(changes['collateral_haircuts'][asset])
                if haircut > 1: raise Block('STRESS: scenario haircut outside 0..1')
                pledged_value += q * positive(changes['collateral_prices'][asset]) * (1 - haircut)
            # Gains never replenish available funding before realisation. Account
            # cash is an explicit gross balance, not broker "available margin".
            available_cash = cash - reserved - number(changes['cash_debit_inr']) + min(pnl, number(0))
            margin = required * positive(changes['margin_multiplier'])
            cash_floor = cash_required * positive(changes['cash_component_multiplier'])
            if cash_floor > margin: raise Block('STRESS: stressed cash component exceeds stressed total')
            total_shortfall = max(number(0), margin - available_cash - pledged_value)
            cash_shortfall = max(number(0), cash_floor - available_cash)
            results.append({'id': key, 'broker': account['broker'], 'account_id': account['account_id'],
                'mark_to_market_change_inr': money(pnl), 'stressed_cash_inr': money(available_cash),
                'collateral_after_haircut_inr': money(pledged_value), 'required_margin_inr': money(margin),
                'total_shortfall_inr': money(total_shortfall), 'cash_component_shortfall_inr': money(cash_shortfall),
                'additional_cash_needed_inr': money(max(total_shortfall, cash_shortfall))})
        output.append({'name': name, 'accounts': results,
            'additional_cash_needed_inr': money(sum((number(r['additional_cash_needed_inr']) for r in results), number(0)))})
    return {'schema': 'portfolio_stress_report_v1', 'as_of': now.isoformat(), 'input_hash': fingerprint(spec),
        'scenarios': output, 'submitted': False, 'execution_eligible': False,
        'limitations': ['Explicit user scenarios, not calibrated forecasts or a SPAN replica.',
            'Cash balances, reserved cash, account margin and cash component must use consistent gross accounting; do not substitute available margin.',
            'Collateral contributes funding only, not duplicate position P&L. Gains are not credited as available cash.',
            'Accounts never offset each other. Unspecified market paths and future broker haircuts remain unknown.']}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True); p.add_argument('--out', required=True)
    p.add_argument('--now', help='Saved-evidence research timestamp only')
    a = p.parse_args(argv)
    try:
        from trader import read
        out = Path(a.out)
        if out.exists(): raise Block('OUTPUT: choose a new file')
        result = evaluate(read(a.input), instant(a.now))
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as f: json.dump(result, f, indent=2, allow_nan=False)
        print(json.dumps({'status': 'SAVED', 'execution_eligible': False})); return 0
    except (Block, ValueError, KeyError, TypeError, OSError, ArithmeticError):
        print('BLOCKED: invalid account, asset, clock or scenario evidence'); return 2


if __name__ == '__main__': raise SystemExit(main())

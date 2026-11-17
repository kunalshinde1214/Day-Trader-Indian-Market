"""Provider-specific, read-only basket estimates; unreported semantics stay unknown."""
from core import Block, fingerprint, number


def identity(broker, instrument):
    if broker == 'dhan':
        from dhan_readonly import security_id, segment_for
        security_id(instrument['token']); segment_for(instrument['exchange'])
    elif broker == 'upstox':
        from upstox_readonly import instrument_key, SEGMENTS
        key = instrument_key(instrument['token'])
        if SEGMENTS[key.split('|')[0]] != instrument['exchange']:
            raise Block('BASKET: Upstox token segment mismatch')
    elif broker == 'angel':
        from angel_readonly import symbol_token
        symbol_token(instrument['token'])
    elif broker == 'fyers':
        from fyers_readonly import token_exchange, full_symbol, symbol_key
        if token_exchange(instrument['token']) != instrument['exchange']:
            raise Block('BASKET: FYERS token exchange mismatch')
        symbol_key(full_symbol(instrument['symbol'], instrument['exchange']))


def estimate(client, broker, orders, instruments, account_id=None):
    if broker == 'kite':
        body = orders
        result = client.request('POST', '/margins/basket', query={'consider_positions': 'true'}, body=body)
        if not isinstance(result, dict): raise Block('BASKET: margin object required')
        rows = result.get('orders')
        if not isinstance(rows, list) or len(rows) != len(orders) or any(not isinstance(r, dict) for r in rows) or any(
                (r.get('exchange'), r.get('tradingsymbol')) != (o['exchange'], o['tradingsymbol'])
                for r, o in zip(rows, orders)):
            raise Block('BASKET: margin response legs mismatch')
        initial, final = number(result['initial']['total']), number(result['final']['total'])
        observed = {'initial_inr': float(initial), 'final_inr': float(final),
                    'required_inr': float(max(initial, final)), 'response_identity_verified': True,
                    'basis': 'Kite initial/final estimates with consider_positions=true'}
    else:
        selected = [instruments[(o['exchange'], o['tradingsymbol'], o['product'])] for o in orders]
        if broker == 'dhan':
            from dhan_readonly import segment_for
            if not isinstance(account_id, str) or not account_id.isascii() or not account_id.isdigit():
                raise Block('BASKET: exact numeric Dhan account identity required')
            products = {'MIS': 'INTRADAY', 'CNC': 'CNC', 'NRML': 'MARGIN'}
            # Official SDK spelling; the web guide also contains a conflicting
            # scripts/includeOrders example. Never retry a different contract.
            body = {'dhanClientId': account_id, 'includePosition': True, 'includeOrder': True, 'scripList': [
                {'exchangeSegment': segment_for(i['exchange']), 'securityId': i['token'],
                 'transactionType': o['transaction_type'], 'quantity': o['quantity'],
                 'productType': products[o['product']], 'price': o['price'], 'triggerPrice': 0}
                for o, i in zip(orders, selected)]}
            result = client.request('POST', '/margincalculator/multi', body=body)
            if not isinstance(result, dict) or result.get('currency') != 'INR':
                raise Block('BASKET: Dhan documented INR response required')
            total = number(result['total_margin'])
            for field in ('span_margin', 'exposure_margin', 'equity_margin', 'fo_margin', 'commodity_margin'):
                number(result[field])
            observed = {'initial_inr': None, 'final_inr': None, 'required_inr': float(total),
                        'account_total_inr': float(total),
                        'basis': 'Dhan total_margin requested with positions and open orders; account-inclusive estimate, not incremental cash requirement',
                        'contract_acceptance': 'UNVERIFIED_ON_USER_ACCOUNT'}
        elif broker == 'upstox':
            body = {'instruments': [{'instrument_key': i['token'], 'quantity': o['quantity'],
                    'transaction_type': o['transaction_type'], 'product': 'I' if o['product'] == 'MIS' else 'D',
                    'price': o['price']} for o, i in zip(orders, selected)]}
            result = client.request('POST', '/v2/charges/margin', body=body)
            if not isinstance(result, dict): raise Block('BASKET: margin object required')
            rows = result.get('margins')
            if not isinstance(rows, list) or len(rows) != len(orders) or any(not isinstance(r, dict) for r in rows):
                raise Block('BASKET: Upstox margin row count mismatch')
            for row in rows: number(row['total_margin'])
            initial, final = number(result['required_margin']), number(result['final_margin'])
            observed = {'initial_inr': float(initial), 'final_inr': float(final),
                        'required_inr': float(max(initial, final)),
                        'basis': 'Upstox required/final margin; existing-position inclusion unverified'}
        elif broker == 'angel':
            from angel_readonly import MARGIN
            products = {'MIS': 'INTRADAY', 'CNC': 'DELIVERY', 'NRML': 'CARRYFORWARD'}
            body = {'positions': [{'exchange': o['exchange'], 'qty': o['quantity'], 'price': o['price'],
                    'productType': products[o['product']], 'token': i['token'],
                    'tradeType': o['transaction_type'], 'orderType': 'LIMIT'} for o, i in zip(orders, selected)]}
            result = client.request('POST', MARGIN, body)
            if not isinstance(result, dict): raise Block('BASKET: margin object required')
            observed = {'initial_inr': None, 'final_inr': None,
                        'required_inr': float(number(result['totalMarginRequired'])),
                        'basis': 'Angel totalMarginRequired; initial/final split and existing-position inclusion unverified'}
        elif broker == 'fyers':
            from fyers_readonly import MARGIN, full_symbol
            products = {'MIS': 'INTRADAY', 'CNC': 'CNC', 'NRML': 'MARGIN'}
            body = {'data': [{'symbol': full_symbol(o['tradingsymbol'], o['exchange']), 'qty': o['quantity'],
                    'side': 1 if o['transaction_type'] == 'BUY' else -1, 'type': 1,
                    'productType': products[o['product']], 'limitPrice': o['price'],
                    'stopPrice': 0, 'stopLoss': 0, 'takeProfit': 0} for o in orders]}
            result = client.request('POST', MARGIN, body=body)['data']
            if not isinstance(result, dict): raise Block('BASKET: margin object required')
            observed = {'initial_inr': None, 'final_inr': None,
                        'required_inr': float(number(result['margin_total'])),
                        'account_total_inr': float(number(result['margin_new_order'])),
                        'available_inr': float(number(result['margin_avail'], minimum='-1e30')),
                        'basis': 'FYERS proposed-order margin; separate account total includes existing positions'}
        else:
            raise Block('BASKET: unsupported provider')
        observed['response_identity_verified'] = False
    return {**observed, 'request_hash': fingerprint(body), 'response_hash': fingerprint(result)}

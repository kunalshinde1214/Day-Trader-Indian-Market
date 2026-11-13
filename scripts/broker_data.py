"""Normalize read-only quote/candle snapshots for the analysis file pipeline."""
from datetime import datetime
from core import Block, IST, instant, fresh, positive, validate_instrument, session_bars


def quote_key(instrument):
    broker=instrument["broker"]
    if broker=="upstox":return instrument["token"]
    if broker=="dhan":
        from dhan_readonly import segment_for
        return segment_for(instrument["exchange"])+":"+instrument["token"]
    if broker=="angel":return instrument["exchange"]+":"+instrument["token"]
    if broker=="fyers":
        from fyers_readonly import full_symbol
        return full_symbol(instrument['symbol'],instrument['exchange'])
    return instrument["exchange"]+":"+instrument["symbol"]


def normalize_quote(result,i,now,max_age):
    key=quote_key(i)
    if result.get("source")!=i["broker"] or key in result.get("missing",[]):raise Block("QUOTE: missing/mismatched source")
    if i["broker"]=="kite":
        row=result["data"][key]
        if str(row["instrument_token"])!=str(i["token"]):raise Block("QUOTE: token mismatch")
        # Kite exchange timestamps without an offset are explicitly documented IST.
        ts=datetime.fromisoformat(row["last_trade_time"])
        if ts.tzinfo is None:ts=ts.replace(tzinfo=IST)
        quote={"source":"kite","token":str(row["instrument_token"]),"exchange":i["exchange"],
               "price":float(positive(row["last_price"])),"as_of":ts.isoformat()}
    else:quote=result["quotes"][key]
    if quote.get("exchange")!=i["exchange"] or str(quote.get("token"))!=str(i["token"]):raise Block("QUOTE: exact token/exchange mismatch")
    if quote.get("symbol",i["symbol"])!=i["symbol"]:raise Block("QUOTE: symbol mismatch")
    fresh(quote.get("as_of"),now,max_age,"quote trade time")
    return {**quote,"symbol":i["symbol"]}


def refresh(client,instrument,now,max_age=60,interval=5):
    if type(interval) is not int or interval not in (1,5):raise Block("INTERVAL: use integer 1 or 5")
    validate_instrument(instrument,now)
    i=instrument
    first=now.replace(hour=9,minute=15,second=0,microsecond=0)
    last=now.replace(minute=(now.minute//interval)*interval,second=0,microsecond=0)
    if last<=first:raise Block("BARS: first completed bar unavailable")
    kwargs={}
    if i["broker"]=="upstox":kwargs["intraday"]=True
    if i["broker"]=="dhan":kwargs["instrument_type"]=i["broker_instrument"]
    name="minute" if interval==1 else "5minute"
    candles=client.candles(i["token"],i["symbol"],i["exchange"],first.isoformat(),last.isoformat(),name,**kwargs)
    if candles.get("interval","5minute")!=name:raise Block("INTERVAL: provider returned a different interval")
    if any(str(candles.get(k))!=str(i[k]) for k in ("broker","exchange","symbol","token")):raise Block("BARS: identity mismatch")
    bars=session_bars(candles["bars"],now,interval)
    quote=normalize_quote(client.quotes([quote_key(i)]),i,now,max_age)
    return {"source":i["broker"],"as_of":candles["as_of"],"interval":name,"bars":bars},quote

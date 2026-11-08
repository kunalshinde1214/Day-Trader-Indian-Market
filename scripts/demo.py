"""Synthetic offline inputs, never real instrument data."""
from datetime import datetime, timedelta
import random
from core import IST


def inputs(config):
    now=datetime(2026,10,30,9,35,tzinfo=IST)
    if config.get("setup") == "gap_and_go":
        now=now.replace(minute=30)
    stamp=now.isoformat()
    token = "NSE_EQ|DEMO00000001" if config["broker"] == "upstox" else "999999999"
    if config["broker"] == "fyers":
        token = "10100000999999"
    instrument={"broker":config["broker"],"symbol":"DEMO-EQ","exchange":"NSE","token":token,"kind":"equity",
        "tick_size":0.05,"lot_size":1,"master_date":now.date().isoformat(),"expiry":None}
    bars=[]
    for k,(o,h,l,c) in enumerate([(100,101,99,100),(100,100.8,99.4,100.2),(100.2,100.9,99.5,100.5),(100.5,102.1,100.4,102)]):
        bars.append({"at":now.replace(hour=9,minute=15+k*5).isoformat(),"open":o,"high":h,"low":l,"close":c,"volume":100000})
    if config.get("setup", "orb") == "vwap_reclaim":
        for b, values in zip(bars, [(100,102,99,101),(101,101,98,99),(99,100,98,99),(99,103,98.5,102)]):
            b.update(zip(("open","high","low","close"),values))
    elif config.get("setup") == "gap_and_go":
        bars=bars[:3]
        for b, values in zip(bars, [(101,101.8,100.5,101.2),(101.2,102,100.8,101.4),(101.4,102.1,101,102)]):
            b.update(zip(("open","high","low","close"),values))
    rng=random.Random(7)
    historical=[]
    d=now.date()-timedelta(days=1)
    days=[]
    while len(days)<25:
        if d.weekday()<5: days.append(d)
        d-=timedelta(days=1)
    for day in sorted(days):
        previous=100.0+rng.uniform(-3,3)
        for k in range(70):
            start=datetime(day.year,day.month,day.day,9,15,tzinfo=IST)+timedelta(minutes=5*k)
            o=previous*(1+rng.uniform(-0.001,0.001))
            c=o*(1+rng.uniform(-0.008,0.008))
            h=max(o,c)*(1+rng.uniform(0.001,0.005));l=min(o,c)*(1-rng.uniform(0.001,0.005))
            historical.append({"at":start.isoformat(),"open":o,"high":h,"low":l,"close":c,"volume":100000})
            previous=c
    source="synthetic_fixture_not_market_data"
    meta={"source":source,"as_of":stamp}
    context={
        "session":{**meta,"date":now.date().isoformat(),"is_open_day":True,"halted":False,"open":"09:15","close":"15:30","broker_squareoff":"15:15"},
        "restrictions":{**meta,"instruments":{"NSE:DEMO-EQ":{k:False for k in ("t2t","asm","gsm","ban","suspended","corporate_action","results_due")}}},
        "event":{**meta,"date":now.date().isoformat(),"risk_multiplier":1,"events":[]},
        "account":{**meta,"broker":config["broker"],"account_id":config["account_id"],"date":now.date().isoformat(),"reconciled":True,"cash_available":100000,"margin_available":100000,
            "daily_risk_used":0,"trades_today":0,"orders_today":0,"position_values":{},"seen_entries":[]},
        "quote":{**meta,"symbol":"DEMO-EQ","exchange":"NSE","price":102}
    }
    history={**meta,"broker":config["broker"],"symbol":"DEMO-EQ","exchange":"NSE","token":token,"interval":"5minute","bars":historical}
    universe={**meta,"scope":"one synthetic instrument","instruments":[{**instrument,"average_daily_turnover_inr":200000000}]}
    candles={**meta,"instruments":{"NSE:DEMO-EQ":{**meta,"bars":bars}}}
    if config.get("setup") == "gap_and_go":
        def fictional_reference(day):
            # Invented weekday-only calendar and close: never a production fallback.
            previous=day-timedelta(days=1)
            while previous.weekday()>=5:
                previous-=timedelta(days=1)
            return {"broker":config["broker"],"symbol":instrument["symbol"],"exchange":instrument["exchange"],"token":token,
                    "for_session":day.isoformat(),"previous_session":previous.isoformat(),"close":100,
                    "available_at":datetime(previous.year,previous.month,previous.day,16,0,tzinfo=IST).isoformat(),
                    "source":source,"calendar_source":"fictional_weekday_calendar_not_exchange_calendar",
                    "price_basis":"unadjusted_session_close","corporate_actions_clear":True}
        candles["instruments"]["NSE:DEMO-EQ"]["prior_close"]=fictional_reference(now.date())
        history["prior_closes"]={day.isoformat():fictional_reference(day) for day in days}
    if config.get("setup") in {"breakout_20d", "bull_flag", "oversold_bounce"}:
        def fictional_daily(day):
            previous=day-timedelta(days=1)
            prior=[]
            while len(prior)<30:
                if previous.weekday()<5:
                    prior.append(previous)
                previous-=timedelta(days=1)
            prior.sort()
            closes=[101.0]*30
            if config["setup"]=="bull_flag":
                closes=[85.0]*15+[85+(k+1)*1.6 for k in range(10)]+[101.0]*5
            elif config["setup"]=="oversold_bounce":
                closes=[130.0-k for k in range(30)]
            daily=[{"date":d.isoformat(),"available_at":datetime(d.year,d.month,d.day,16,0,tzinfo=IST).isoformat(),
                    "open":v,"high":v+0.2,"low":v-0.2,"close":v,"volume":100000} for d,v in zip(prior,closes)]
            return {"broker":config["broker"],"symbol":instrument["symbol"],"exchange":instrument["exchange"],"token":token,
                    "for_session":day.isoformat(),"previous_session":prior[-1].isoformat(),"sessions":[d.isoformat() for d in prior],
                    "source":source,"calendar_source":"fictional_weekday_calendar_not_exchange_calendar",
                    "price_basis":"unadjusted_session_ohlc","corporate_actions_clear":True,"bars":daily}
        candles["instruments"]["NSE:DEMO-EQ"]["daily_reference"]=fictional_daily(now.date())
        history["daily_references"]={day.isoformat():fictional_daily(day) for day in days}
    return now,instrument,context,history,universe,candles

"""Allowlisted, credential-redacting Kite data adapter. No execution routes."""
from __future__ import annotations
import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from core import Block, instant, fingerprint, positive, integer


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Block("KITE_REDIRECT: refused to forward authorization headers")


class KiteReadOnly:
    def __init__(self, opener=None):
        self.opener=opener

    def request(self, method, path, query=None, body=None, csv_result=False):
        allowed = method == "GET" and (path in {"/instruments","/quote","/user/profile","/user/margins","/portfolio/holdings","/portfolio/positions","/orders","/trades"} or re.fullmatch(r"/instruments/historical/[0-9]+/(5minute|day)",path))
        allowed = allowed or (method == "POST" and path == "/margins/orders")
        if not allowed:
            raise Block("READ_ONLY: endpoint/method not allowed; live execution unavailable")
        api_key=os.environ.get("KITE_API_KEY")
        access_token=os.environ.get("KITE_ACCESS_TOKEN")
        if not api_key or not access_token:
            raise Block("KITE_AUTH: configure KITE_API_KEY and KITE_ACCESS_TOKEN locally; never send them in chat")
        url="https://api.kite.trade"+path
        if query:
            url+="?"+urllib.parse.urlencode(query,doseq=True)
        request=urllib.request.Request(url, data=json.dumps(body,allow_nan=False).encode() if body is not None else None,
            method=method,headers={"X-Kite-Version":"3","Authorization":f"token {api_key}:{access_token}","Content-Type":"application/json"})
        try:
            opener=self.opener or urllib.request.build_opener(NoRedirect()).open
            with opener(request,timeout=20) as response:
                raw=response.read().decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            raise Block(f"KITE_HTTP_{exc.code}: request failed; no automatic retry. Check session, subscription and rate limit locally.") from None
        except (urllib.error.URLError,TimeoutError,OSError):
            raise Block("KITE_NETWORK: data unavailable; no automatic retry") from None
        if csv_result:
            return raw
        try:
            data=json.loads(raw)
            if data.get("status")!="success":
                raise Block("KITE_RESPONSE: broker returned a failure; body suppressed")
            return data["data"]
        except (ValueError,KeyError,AttributeError):
            raise Block("KITE_RESPONSE: invalid broker response") from None

    def instruments(self):
        raw=self.request("GET","/instruments",csv_result=True)
        rows=list(csv.DictReader(io.StringIO(raw)))
        if not rows or not {"instrument_token","tradingsymbol","lot_size","tick_size","exchange","instrument_type"} <= rows[0].keys():
            raise Block("KITE_MASTER: missing required columns")
        today=instant().date().isoformat()
        normalized=[]
        for r in rows:
            kind={"EQ":"equity","FUT":"future","CE":"option","PE":"option"}.get(r["instrument_type"])
            if not kind or r["exchange"] not in {"NSE","BSE","NFO","BFO"} or r.get("segment")=="INDICES":
                continue
            normalized.append({"broker":"kite","symbol":r["tradingsymbol"],"token":str(r["instrument_token"]),"exchange":r["exchange"],
                "kind":kind,"lot_size":integer(r["lot_size"]),"tick_size":float(positive(r["tick_size"])),
                "expiry":r.get("expiry") or None,"master_date":today,
                "underlying_type":"unknown" if kind!="equity" else None})
        return {"source":"kite","as_of":instant().isoformat(),"instruments":normalized,
                "note":"Instrument master does not establish Nifty500 membership, surveillance clearance or underlying type."}

    def candles(self, token, symbol, exchange, start, end, interval="5minute"):
        token=integer(token,"instrument token")
        a,b=instant(start),instant(end)
        if a>=b or interval not in {"5minute","day"}:
            raise Block("HISTORY_RANGE: invalid interval/date range")
        data=self.request("GET",f"/instruments/historical/{token}/{interval}",query={"from":a.strftime("%Y-%m-%d %H:%M:%S"),"to":b.strftime("%Y-%m-%d %H:%M:%S"),"oi":1})
        bars=[]
        for r in data["candles"]:
            if len(r)<6:
                raise Block("KITE_CANDLES: incomplete row")
            bars.append(dict(zip(("at","open","high","low","close","volume"),r[:6])))
        return {"source":"kite","broker":"kite","as_of":instant().isoformat(),"symbol":symbol,"exchange":exchange,"token":str(token),"interval":interval,"bars":bars}

    def quotes(self, symbols):
        if not 1<=len(symbols)<=500 or not all(re.fullmatch(r"(NSE|BSE|NFO|BFO):[^\s:]+",s) for s in symbols):
            raise Block("QUOTE_INPUT: provide 1-500 EXCHANGE:SYMBOL identifiers")
        data=self.request("GET","/quote",query={"i":symbols})
        return {"source":"kite","fetched_at":instant().isoformat(),"requested":symbols,"missing":[s for s in symbols if s not in data],"data":data}

    def account(self):
        # Raw read-only snapshots: not automatically marked as a reconciled risk ledger.
        return {"source":"kite","broker":"kite","account_id":self.account_id(),"as_of":instant().isoformat(),"margins":self.request("GET","/user/margins"),
            "positions":self.request("GET","/portfolio/positions"),"orders":self.request("GET","/orders"),"reconciled":False}

    def account_id(self):
        value = self.request("GET", "/user/profile").get("user_id")
        if not isinstance(value, str) or not value.strip():
            raise Block("KITE_PROFILE: missing account identity")
        return value

    def margin(self, order_payload):
        required={"exchange","tradingsymbol","transaction_type","quantity","product","order_type","price","validity"}
        if set(order_payload)!=required:
            raise Block("MARGIN_PAYLOAD: use exact kite_payload_preview from a sized order")
        integer(order_payload["quantity"])
        positive(order_payload["price"])
        data=self.request("POST","/margins/orders",body=[{**order_payload,"variety":"regular"}])
        if not isinstance(data,list) or len(data)!=1:
            raise Block("MARGIN_RESPONSE: expected one margin result")
        row=data[0]
        if row["tradingsymbol"]!=order_payload["tradingsymbol"] or row["exchange"]!=order_payload["exchange"]:
            raise Block("MARGIN_RESPONSE: instrument mismatch")
        return {"source":"kite","account_id":self.account_id(),"as_of":instant().isoformat(),"payload_hash":fingerprint(order_payload),
                "required_inr":float(positive(row["total"],"required margin"))}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("command",choices=["instruments","candles","quotes","account","margin"])
    p.add_argument("--out",required=True)
    p.add_argument("--symbols",nargs="+")
    p.add_argument("--token");p.add_argument("--symbol");p.add_argument("--exchange")
    p.add_argument("--from",dest="start");p.add_argument("--to",dest="end")
    p.add_argument("--interval",choices=["5minute","day"],default="5minute")
    p.add_argument("--payload")
    a=p.parse_args(argv)
    try:
        output=Path(a.out)
        if output.exists():
            raise Block("OUTPUT: choose a new snapshot filename; existing files are not overwritten")
        k=KiteReadOnly()
        if a.command=="instruments": result=k.instruments()
        elif a.command=="candles":
            if not all((a.token,a.symbol,a.exchange,a.start,a.end)): raise Block("INPUT: candles needs token, symbol, exchange, from, to")
            result=k.candles(a.token,a.symbol,a.exchange,a.start,a.end,a.interval)
        elif a.command=="quotes": result=k.quotes(a.symbols or [])
        elif a.command=="account": result=k.account()
        else:
            if not a.payload: raise Block("INPUT: margin needs --payload")
            result=k.margin(json.loads(Path(a.payload).read_text(encoding="utf-8-sig")))
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open("x",encoding="utf-8") as f: json.dump(result,f,indent=2,allow_nan=False)
        print(json.dumps({"status":"saved","path":str(output),"source":"kite","orders_submitted":0}))
        return 0
    except Block as exc:
        print(json.dumps({"status":"BLOCK","reason":str(exc),"orders_submitted":0}))
        return 2
    except (ValueError,KeyError,TypeError,OSError):
        # Exceptions can contain identifiers or paths, but never dump raw API bodies or headers.
        print(json.dumps({"status":"BLOCK","reason":"Kite read failed or inputs invalid. Verify local credentials, input files, subscription, and output path. No order was submitted."}))
        return 2


if __name__=="__main__":
    sys.exit(main())

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from core import Block
from kite_readonly import KiteReadOnly, NoRedirect
from trader import main


class Response(io.BytesIO):
    pass


class KiteTests(unittest.TestCase):
    def test_mutation_endpoints_rejected_before_credentials_or_network(self):
        calls=[]
        k=KiteReadOnly(lambda *a,**kw:calls.append(a))
        for method,path in (("POST","/orders/regular"),("PUT","/orders/regular/1"),("DELETE","/orders/regular/1"),("GET","https://evil.test"),("POST","/session/token")):
            with self.subTest(method=method),self.assertRaisesRegex(Block,"READ_ONLY"): k.request(method,path)
        self.assertEqual(calls,[])

    def test_missing_credentials_no_network(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(Block,"KITE_AUTH"):
            KiteReadOnly().request("GET","/instruments")

    def test_http_failure_redacts_secret_and_does_not_retry(self):
        calls=[]
        def opener(req,**kwargs):
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url,403,"PRIVATE_TOKEN",{},None)
        with patch.dict(os.environ,{"KITE_API_KEY":"PRIVATE_KEY","KITE_ACCESS_TOKEN":"PRIVATE_TOKEN"}):
            with self.assertRaises(Block) as exc: KiteReadOnly(opener).request("GET","/quote")
        self.assertNotIn("PRIVATE",str(exc.exception));self.assertEqual(len(calls),1)

    def test_quotes_reports_missing_symbols(self):
        def opener(req,**kwargs):
            self.assertEqual(req.get_method(),"GET")
            self.assertIn("i=NSE%3ADEMO",req.full_url)
            return Response(b'{"status":"success","data":{"NSE:DEMO":{"last_price":100}}}')
        with patch.dict(os.environ,{"KITE_API_KEY":"test","KITE_ACCESS_TOKEN":"test"}):
            result=KiteReadOnly(opener).quotes(["NSE:DEMO","NSE:MISSING"])
        self.assertEqual(result["missing"],["NSE:MISSING"])

    def test_redirect_does_not_forward_auth(self):
        with self.assertRaisesRegex(Block,"REDIRECT"):
            NoRedirect().redirect_request(None,None,302,"",{},"https://other.example")

    def test_master_skips_index_zero_tick(self):
        csv="instrument_token,tradingsymbol,lot_size,tick_size,exchange,instrument_type,expiry,segment\n1,DEMO,1,0.05,NSE,EQ,,NSE\n2,INDEX,0,0,NSE,EQ,,INDICES\n"
        with patch.dict(os.environ,{"KITE_API_KEY":"test","KITE_ACCESS_TOKEN":"test"}):
            result=KiteReadOnly(lambda *a,**k:Response(csv.encode())).instruments()
        self.assertEqual(len(result["instruments"]),1)
        self.assertEqual(result["instruments"][0]["lot_size"],1)


class CliTests(unittest.TestCase):
    def run_cli(self,args):
        output=io.StringIO()
        with contextlib.redirect_stdout(output): code=main(args)
        return code,json.loads(output.getvalue())

    def test_demo_end_to_end_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            code,r=self.run_cli(["demo","--workdir",d])
            self.assertEqual(code,0);self.assertEqual(r["risk"],"PASS")
            for name in ("watchlist.json","plan.json","replay.json","orders.json","state/risk.json","ticket.json"):
                self.assertTrue((Path(d)/name).exists(),name)
            code,r=self.run_cli(["demo","--workdir",d])
            self.assertEqual(code,2);self.assertIn("empty",r["reason"])

    def test_init_does_not_overwrite_user_config(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_cli(["init","--workdir",d])[0],0)
            p=Path(d)/"config.json";p.write_text('{"user":"unchanged"}')
            self.assertEqual(self.run_cli(["init","--workdir",d])[0],2)
            self.assertEqual(json.loads(p.read_text()),{"user":"unchanged"})

    def test_missing_inputs_explicit_block(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_cli(["init","--workdir",d])
            self.assertEqual(self.run_cli(["risk","--workdir",d])[0],2)


if __name__=="__main__": unittest.main()

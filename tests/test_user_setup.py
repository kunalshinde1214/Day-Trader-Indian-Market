import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import BROKERS,Block
from user_setup import profile,main


class SetupTests(unittest.TestCase):
    def test_every_broker_starts_disabled_and_unaccepted(self):
        for broker in BROKERS:
            result=profile(broker,'LOCAL-ID')
            for flag in ('enabled','new_entries','mixed_inventory_reducing_exits','unattended_protection'):self.assertFalse(result[flag])
            for key in ('acceptance','mixed_inventory_acceptance','automation_acceptance'):
                self.assertEqual(result[key]['status'],'NOT_TESTED');self.assertEqual(result[key]['broker'],broker)

    def test_existing_profile_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory,patch('sys.stdout',new_callable=io.StringIO):
            path=Path(directory)/'profile.json';args=['--broker','upstox','--account-id','LOCAL-ID','--out',str(path)]
            self.assertEqual(main(args),0);before=path.read_bytes()
            self.assertEqual(main(args),2);self.assertEqual(path.read_bytes(),before)

    def test_invalid_account_and_broker(self):
        for broker,account in [('other','id'),('kite',''),('kite','has whitespace')]:
            with self.assertRaises(Block):profile(broker,account)


if __name__=='__main__':unittest.main()

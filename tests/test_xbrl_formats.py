from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from core import Block,instant
from research import xbrl
from test_public_workflows import filing


class XbrlFormatsTests(unittest.TestCase):
    now=instant('2026-11-15T10:00:00+05:30')
    def body(self,value,fmt='numdotdecimalin',namespace='2015-02-26',attrs=''):
        raw=filing([('a','2026-04-01','2026-06-30','100')]).decode()
        raw=raw.replace('<xbrl>','<xbrl xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/'+namespace+'">')
        raw=raw.replace('decimals="-6"','decimals="-6" format="ixt:'+fmt+'" '+attrs)
        return raw.replace('>100<','>'+value+'<').encode()
    def value(self,raw):return xbrl(raw,'https://www.nseindia.in/fixture',self.now)['facts'][0]['value']

    def test_indian_grouping_spaces_and_explicit_scale_sign(self):
        for raw in ('1,23,456.50','1 23 456.50','1\u00a023\u00a0456.50','123456.50'):
            self.assertEqual(self.value(self.body(raw)),'123456.50')
        self.assertEqual(self.value(self.body('1,23,456',attrs='scale="2" sign="-"')),'-12345600')

    def test_invalid_grouping_signs_and_exponents_rejected(self):
        for raw in ('123,456','1,234,567','1,23,45','-1,23,456','+123','1e6','NaN','123.'):
            with self.subTest(raw=raw),self.assertRaises(Block):self.value(self.body(raw))

    def test_standard_grouping_remains_distinct(self):
        self.assertEqual(self.value(self.body('1,234,567.5','num-dot-decimal','2022-02-16')),'1234567.5')
        with self.assertRaises(Block):self.value(self.body('1,23,456','num-dot-decimal','2022-02-16'))

    def test_unknown_unbound_and_rebound_transform_namespace_not_guessed(self):
        for raw in (self.body('1,23,456',namespace='future'),
                    self.body('1,23,456').replace(b'format="ixt:',b'format="unknown:'),
                    self.body('1,23,456').replace(b'<Revenue ',b'<Revenue xmlns:ixt="https://evil.invalid/" ')):
            with self.assertRaises(Block):self.value(raw)

    def test_inline_continuations_and_exclusions_are_not_silently_concatenated(self):
        for raw in (self.body('123',attrs='continuedAt="rest"'),self.body('1<exclude>99</exclude>23')):
            with self.assertRaises(Block):self.value(raw)


if __name__=='__main__':unittest.main()

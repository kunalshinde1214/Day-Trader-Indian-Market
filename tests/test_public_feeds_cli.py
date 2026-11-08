import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import instant
from data import main, SOURCES


class PublicFeedCLITests(unittest.TestCase):
    def test_each_membership_source_uses_csv_and_correct_scope(self):
        body = b"Symbol,Series,ISIN Code\nDEMO,EQ,SYNTHETIC\n"
        for source, label in (("nifty50", "Nifty50"), ("niftybank", "BankNifty"), ("nifty500", "Nifty500")):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "members.json"
                with patch("data.fetch", return_value=body) as fetch, patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(main(["collect", "--source", source, "--out", str(out)]), 0)
                result = json.loads(out.read_text())
                self.assertEqual(result["members"][0]["symbol"], "DEMO")
                self.assertEqual(result["scope"], label + " source membership")
                self.assertEqual(result["source"], SOURCES[source])
                self.assertIsNone(result["membership_effective_date"])
                fetch.assert_called_once_with(source)

    def test_html_response_cannot_be_published_as_constituents(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "members.json"
            with patch("data.fetch", return_value=b"<html>unavailable</html>"), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main(["collect", "--source", "nifty50", "--out", str(out)]), 2)
            self.assertFalse(out.exists())

    def test_retrieval_time_is_recorded_after_fetch(self):
        before = instant("2026-11-06T10:00:00+05:30")
        after = instant("2026-11-06T10:00:05+05:30")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "members.json"
            with patch("data.instant", side_effect=[before, after]), patch("data.fetch", return_value=b"Symbol,Series,ISIN Code\nDEMO,EQ,SYNTHETIC\n"), patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main(["collect", "--source", "niftybank", "--out", str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())["as_of"], after.isoformat())


if __name__ == "__main__": unittest.main()

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from release import release_files,static_checks,package


class ReleaseTests(unittest.TestCase):
    def test_private_outputs_research_dependencies_and_caches_are_excluded(self):
        files=[p.relative_to(ROOT).as_posix() for p in release_files()]
        for name in files:
            self.assertFalse(any(v in name.split('/') for v in ('research','test-output','__pycache__','.env','trader')))
        self.assertIn('third-party/day-trader-skill.LICENSE',files)
        self.assertIn('references/operations.md',files)

    def test_packaged_links_and_source_syntax(self):
        self.assertGreater(static_checks()["local_links"],50)

    def test_archive_manifest_hashes_and_determinism(self):
        with tempfile.TemporaryDirectory() as d:
            a,b=Path(d)/"a.zip",Path(d)/"b.zip"
            one=package(a);two=package(b)
            self.assertEqual(one["sha256"],two["sha256"])
            with zipfile.ZipFile(a) as z:
                manifest=json.loads(z.read('india-day-trader/RELEASE-MANIFEST.json'))
                self.assertFalse(manifest['full_specification_complete'])
                for name,digest in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(z.read('india-day-trader/'+name)).hexdigest(),digest)
            with self.assertRaises(FileExistsError):package(a)


if __name__=="__main__":unittest.main()

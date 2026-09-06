import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "verify_handoff", Path(__file__).resolve().parents[1] / "verify_handoff.py"
)
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "run" / "handoff").mkdir(parents=True)
        self.payload = self.root / "run" / "traffic-requests.jsonl"
        self.payload.write_bytes(b'{"event":"recorded"}\n')
        self.entry = {
            "path": "run/traffic-requests.jsonl",
            "sha256": hashlib.sha256(self.payload.read_bytes()).hexdigest(),
        }
        self.write_manifest([self.entry])

    def write_manifest(self, entries):
        (self.root / handoff.MANIFEST).write_text(
            json.dumps({"schema_version": 1, "files": entries}), encoding="utf-8"
        )

    def test_matching_files(self):
        self.assertEqual(handoff.verify(self.root), [])

    def test_changed_file(self):
        self.payload.write_bytes(b"changed")
        self.assertEqual(handoff.verify(self.root), ["CHANGED: run/traffic-requests.jsonl"])

    def test_missing_file(self):
        self.payload.unlink()
        self.assertEqual(handoff.verify(self.root), ["MISSING: run/traffic-requests.jsonl"])

    def test_missing_manifest(self):
        (self.root / handoff.MANIFEST).unlink()
        with self.assertRaisesRegex(ValueError, "Missing"):
            handoff.verify(self.root)

    def test_rejects_empty_and_duplicate_entries(self):
        for entries in ([], [self.entry, self.entry]):
            with self.subTest(entries=entries):
                self.write_manifest(entries)
                with self.assertRaises(ValueError):
                    handoff.verify(self.root)

    def test_rejects_paths_outside_demo(self):
        for path in ("../outside", "/outside", "C:/outside", "run\\..\\outside"):
            with self.subTest(path=path):
                self.write_manifest([{**self.entry, "path": path}])
                with self.assertRaises(ValueError):
                    handoff.verify(self.root)

    def test_rejects_invalid_hash(self):
        self.write_manifest([{**self.entry, "sha256": "invalid"}])
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            handoff.verify(self.root)


if __name__ == "__main__":
    unittest.main()

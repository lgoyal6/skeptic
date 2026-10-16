"""Check the public demo bundle against Skeptic's real recorded-date rule."""

from pathlib import Path
import unittest

from scripts.export_date_demo import build_evidence


class DateDemoExportTests(unittest.TestCase):
    def test_saved_cases_keep_mismatch_abstention_and_later_match(self) -> None:
        root = Path(__file__).resolve().parents[1]
        data = build_evidence(root, "test-source")

        self.assertEqual(
            [row["outcome"] for row in data["cases"]],
            ["contradicts_doc", "unobservable", "supports_doc"],
        )
        self.assertEqual(data["cases"][0]["status"], 200)
        self.assertEqual(data["cases"][0]["requestDate"], "2026-09-13")
        self.assertEqual(data["cases"][0]["responseDate"], "2026-09-11")
        self.assertEqual(data["cases"][1]["requestDate"], data["cases"][1]["responseDate"])
        self.assertEqual(data["cases"][2]["requestDate"], data["cases"][2]["responseDate"])

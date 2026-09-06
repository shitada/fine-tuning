import json
import re
import sys
import unittest
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[1]
TRACES_DIR = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(SOURCE_DIR))

from tools import ALL_TOOLS  # noqa: E402


class ContractTests(unittest.TestCase):
    def test_packaged_contract_matches_approved_fixtures(self) -> None:
        for name in ("zava_system_prompt.md", "zava_tools.json"):
            approved = (TRACES_DIR / "fixtures" / name).read_bytes()
            packaged = (SOURCE_DIR / "contract" / name).read_bytes()
            self.assertEqual(approved, packaged, name)

    def test_function_tools_match_json_contract(self) -> None:
        contract = json.loads(
            (SOURCE_DIR / "contract" / "zava_tools.json").read_text(encoding="utf-8")
        )
        expected = {entry["function"]["name"]: entry["function"] for entry in contract}
        self.assertEqual([function_tool.name for function_tool in ALL_TOOLS], list(expected))
        for function_tool in ALL_TOOLS:
            definition = expected[function_tool.name]
            self.assertRegex(function_tool.name, r"^[A-Za-z0-9_-]{1,64}$")
            self.assertEqual(function_tool.description, definition["description"])
            self.assertEqual(function_tool.parameters(), definition["parameters"])
            self.assertEqual(function_tool.approval_mode, "never_require")
        calculate_items = expected["calculate_resolution"]["parameters"]["properties"][
            "items"
        ]["items"]
        self.assertEqual(
            set(calculate_items["required"]),
            {"item_id", "actions", "reason", "policy_id"},
        )
        self.assertIn(
            "calculation_id",
            expected["submit_resolution"]["parameters"]["required"],
        )

    def test_descriptions_are_japanese(self) -> None:
        japanese = re.compile(r"[ぁ-んァ-ヶ一-龠]")
        for function_tool in ALL_TOOLS:
            self.assertRegex(function_tool.description, japanese)


if __name__ == "__main__":
    unittest.main()

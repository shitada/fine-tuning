import sys
import unittest
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_DIR))

from synthetic_store import (  # noqa: E402
    _INVENTORY_CHECKS,
    _POLICY_CHECKS,
    build_order_id,
    calculate_resolution,
    check_inventory,
    check_resolution_policy,
    get_fulfillment_status,
    get_order_details,
    submit_resolution,
)


def find_order_id(status: str, *, late: bool | None = None) -> str:
    for index in range(1, 100):
        order_id = build_order_id(index)
        fulfillment = get_fulfillment_status(order_id)
        if fulfillment["status"] != status:
            continue
        if late is not None and fulfillment["late_delivery"] != late:
            continue
        return order_id
    raise AssertionError(f"条件に一致する注文がありません: {status}")


class SyntheticStoreTests(unittest.TestCase):
    def test_order_is_deterministic_and_contains_no_pii(self) -> None:
        order = get_order_details("ORD-0001")
        self.assertEqual(order, get_order_details("ORD-0001"))
        self.assertNotIn("email", order["customer"])
        self.assertNotIn("address", order["customer"])
        self.assertEqual(order["currency"], "JPY")
        self.assertTrue(all(item["name"] for item in order["items"]))

    def test_lost_package_allows_replacement_or_refund(self) -> None:
        order_id = "ORD-0003"
        item = get_order_details(order_id)["items"][1]
        self.assertEqual(item["category"], "電子機器")
        policy = check_resolution_policy(order_id, item["item_id"], "未着")
        self.assertEqual(policy["eligible_actions"], ["代替品発送", "返金"])
        result = calculate_resolution(
            order_id,
            [
                {
                    "item_id": item["item_id"],
                    "actions": ["返金"],
                    "reason": "未着",
                    "policy_id": policy["policy_id"],
                }
            ],
        )
        self.assertEqual(result["total_restocking_fee_jpy"], 0)

    def test_late_delivery_credit_combines_with_refund(self) -> None:
        order_id = find_order_id("配達済み", late=True)
        item = get_order_details(order_id)["items"][0]
        policy = check_resolution_policy(order_id, item["item_id"], "破損")
        self.assertIn("返金", policy["eligible_actions"])
        self.assertIn("配送遅延クレジット", policy["eligible_actions"])
        result = calculate_resolution(
            order_id,
            [
                {
                    "item_id": item["item_id"],
                    "actions": ["返金", "配送遅延クレジット"],
                    "reason": "破損",
                    "policy_id": policy["policy_id"],
                }
            ],
        )
        self.assertEqual(result["total_credit_jpy"], 1000)
        self.assertGreater(result["total_refund_jpy"], 0)

    def test_pending_order_allows_cancellation(self) -> None:
        order_id = find_order_id("受付済み")
        item = get_order_details(order_id)["items"][0]
        policy = check_resolution_policy(order_id, item["item_id"], "お客様都合")
        self.assertEqual(policy["eligible_actions"], ["キャンセル"])

    def test_inventory_and_submission_are_deterministic(self) -> None:
        order_id = find_order_id("受付済み")
        order = get_order_details(order_id)
        item = order["items"][0]
        inventory = check_inventory(item["sku"])
        self.assertIn(inventory["message"], {"交換可能です。", "現在は在庫切れです。"})
        policy = check_resolution_policy(order_id, item["item_id"], "お客様都合")
        calculation = calculate_resolution(
            order_id,
            [
                {
                    "item_id": item["item_id"],
                    "actions": ["キャンセル"],
                    "reason": "お客様都合",
                    "policy_id": policy["policy_id"],
                }
            ],
        )
        first = submit_resolution(
            order_id,
            calculation["calculation_id"],
            "キャンセルと返金内容を確認しました。",
        )
        second = submit_resolution(
            order_id,
            calculation["calculation_id"],
            "キャンセルと返金内容を確認しました。",
        )
        self.assertEqual(first, second)
        self.assertFalse(first["external_side_effect"])
        self.assertEqual(first["status"], "処理シミュレーション完了")

    def test_submit_requires_prior_calculation(self) -> None:
        with self.assertRaisesRegex(ValueError, "calculate_resolution"):
            submit_resolution("ORD-0001", "CALC-UNKNOWN", "返金します。")

    def test_duplicate_items_are_rejected(self) -> None:
        order_id = find_order_id("受付済み")
        item = get_order_details(order_id)["items"][0]
        policy = check_resolution_policy(order_id, item["item_id"], "お客様都合")
        requested = {
            "item_id": item["item_id"],
            "actions": ["キャンセル"],
            "reason": "お客様都合",
            "policy_id": policy["policy_id"],
        }
        with self.assertRaisesRegex(ValueError, "重複"):
            calculate_resolution(order_id, [requested, requested])

    def test_invalid_exchange_sku_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "SKU"):
            check_inventory("SKU-001-999")

    def test_calculation_rejects_unissued_policy_id(self) -> None:
        order_id = find_order_id("受付済み")
        item = get_order_details(order_id)["items"][0]
        policy = check_resolution_policy(order_id, item["item_id"], "お客様都合")
        _POLICY_CHECKS.pop(policy["policy_id"])
        with self.assertRaisesRegex(ValueError, "check_resolution_policy"):
            calculate_resolution(
                order_id,
                [
                    {
                        "item_id": item["item_id"],
                        "actions": ["キャンセル"],
                        "reason": "お客様都合",
                        "policy_id": policy["policy_id"],
                    }
                ],
            )

    def test_exchange_rejects_unissued_inventory_check_id(self) -> None:
        order_id = "ORD-0001"
        item = get_order_details(order_id)["items"][0]
        policy = check_resolution_policy(order_id, item["item_id"], "サイズ違い")
        sku_prefix = item["sku"].rsplit("-", 1)[0]
        inventory = None
        for variant in range(1, 4):
            candidate = check_inventory(f"{sku_prefix}-{variant}")
            if candidate["available"]:
                inventory = candidate
                break
        self.assertIsNotNone(inventory)
        _INVENTORY_CHECKS.pop(inventory["inventory_check_id"])
        with self.assertRaisesRegex(ValueError, "check_inventory"):
            calculate_resolution(
                order_id,
                [
                    {
                        "item_id": item["item_id"],
                        "actions": ["交換"],
                        "reason": "サイズ違い",
                        "policy_id": policy["policy_id"],
                        "replacement_sku": inventory["sku"],
                        "inventory_check_id": inventory["inventory_check_id"],
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()

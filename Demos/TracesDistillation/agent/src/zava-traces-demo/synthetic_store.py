from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any


REFERENCE_DATE = date(2026, 8, 31)
ORDER_ID_PATTERN = re.compile(r"^ORD-(\d{3,6})$")
SKU_PATTERN = re.compile(r"^SKU-(\d{3})-([1-3])$")
REASONS = {
    "破損",
    "不良",
    "サイズ違い",
    "色違い",
    "誤配送",
    "お客様都合",
    "未着",
    "遅配",
}
ACTIONS = {
    "返金",
    "交換",
    "代替品発送",
    "キャンセル",
    "ストアクレジット",
    "配送遅延クレジット",
    "対象外",
}
TIERS = ("標準", "ゴールド", "プラチナ")

PRODUCTS = (
    {"name": "ワイヤレスヘッドホン", "category": "電子機器", "base_price": 12800},
    {"name": "ランニングシューズ", "category": "衣料品", "base_price": 8900},
    {"name": "電気ケトル", "category": "電子機器", "base_price": 6500},
    {"name": "オフィスチェア", "category": "生活雑貨", "base_price": 24800},
    {"name": "ウールセーター", "category": "衣料品", "base_price": 9800},
    {"name": "ヨガマット", "category": "生活雑貨", "base_price": 4200},
    {"name": "スマートウォッチ", "category": "電子機器", "base_price": 29800},
    {"name": "保湿クリーム", "category": "パーソナルケア", "base_price": 3200},
)
_CALCULATIONS: dict[str, dict[str, Any]] = {}
_POLICY_CHECKS: dict[str, dict[str, Any]] = {}
_INVENTORY_CHECKS: dict[str, dict[str, Any]] = {}


def _order_number(order_id: str) -> int:
    match = ORDER_ID_PATTERN.fullmatch(order_id)
    if not match:
        raise ValueError("注文IDはORD-001のような形式で指定してください。")
    return int(match.group(1))


def _stable_number(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:12], 16)


def _reference_id(prefix: str, payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}-{digest}"


def _remember(registry: dict[str, dict[str, Any]], key: str, value: dict[str, Any]) -> None:
    registry[key] = value
    if len(registry) > 2000:
        registry.pop(next(iter(registry)))


def _sku_details(sku: str) -> dict[str, Any]:
    match = SKU_PATTERN.fullmatch(sku)
    if not match:
        raise ValueError("SKUはSKU-001-1のような形式で指定してください。")
    product_index = int(match.group(1))
    variant = int(match.group(2))
    if not 1 <= product_index <= len(PRODUCTS):
        raise ValueError("指定されたSKUはsynthetic catalogに存在しません。")
    product = PRODUCTS[product_index - 1]
    return {
        "sku": sku,
        "product_index": product_index,
        "variant": variant,
        "name": product["name"],
        "category": product["category"],
        "unit_price_jpy": product["base_price"] + variant * 100,
    }


def build_order_id(index: int) -> str:
    if index < 1:
        raise ValueError("注文番号は1以上で指定してください。")
    return f"ORD-{index:04d}"


def _item_for(order_number: int, position: int) -> dict[str, Any]:
    product_index = (order_number + position * 3) % len(PRODUCTS)
    variant = (order_number + position) % 3 + 1
    sku = f"SKU-{product_index + 1:03d}-{variant}"
    sku_details = _sku_details(sku)
    return {
        "item_id": f"ITEM-{order_number:04d}-{position + 1}",
        "sku": sku,
        "name": sku_details["name"],
        "category": sku_details["category"],
        "unit_price_jpy": sku_details["unit_price_jpy"],
        "quantity": 1,
        "final_sale": order_number % 11 == 0,
        "sealed": not (
            sku_details["category"] == "パーソナルケア" and order_number % 5 == 0
        ),
    }


def get_order_details(order_id: str) -> dict[str, Any]:
    order_number = _order_number(order_id)
    item_count = 2 if order_number % 3 == 0 else 1
    items = [_item_for(order_number, position) for position in range(item_count)]
    return {
        "order_id": order_id,
        "customer": {"loyalty_tier": TIERS[order_number % len(TIERS)]},
        "items": items,
        "payment_method": "元の支払方法",
        "currency": "JPY",
        "order_date": (REFERENCE_DATE - timedelta(days=20 + order_number % 50)).isoformat(),
        "total_jpy": sum(
            item["unit_price_jpy"] * item["quantity"] for item in items
        ),
    }


def get_fulfillment_status(order_id: str) -> dict[str, Any]:
    order_number = _order_number(order_id)
    scenario = order_number % 8
    status = {
        0: "配達済み",
        1: "配達済み",
        2: "配達済み",
        3: "紛失",
        4: "配達済み",
        5: "受付済み",
        6: "処理中",
        7: "発送済み",
    }[scenario]
    days_since_delivery = 5 + order_number % 55
    delivery_date = (
        REFERENCE_DATE - timedelta(days=days_since_delivery)
        if status == "配達済み"
        else None
    )
    days_late = 4 if scenario == 4 else 0
    promised_date = (
        delivery_date - timedelta(days=days_late)
        if delivery_date
        else REFERENCE_DATE + timedelta(days=3)
    )
    return {
        "order_id": order_id,
        "status": status,
        "promised_date": promised_date.isoformat(),
        "delivery_date": delivery_date.isoformat() if delivery_date else None,
        "late_delivery": days_late > 2,
        "days_late": days_late,
        "days_since_delivery": days_since_delivery if delivery_date else None,
        "items": [
            {"item_id": item["item_id"], "status": status}
            for item in get_order_details(order_id)["items"]
        ],
    }


def _find_item(order: dict[str, Any], item_id: str) -> dict[str, Any]:
    for item in order["items"]:
        if item["item_id"] == item_id:
            return item
    raise ValueError("指定された商品IDは注文に含まれていません。")


def _return_window_days(tier: str, category: str) -> int:
    if category in {"衣料品", "生活雑貨"}:
        return {"標準": 30, "ゴールド": 45, "プラチナ": 60}[tier]
    return {"標準": 15, "ゴールド": 30, "プラチナ": 45}[tier]


def check_resolution_policy(
    order_id: str,
    item_id: str,
    reason: str,
) -> dict[str, Any]:
    if reason not in REASONS:
        raise ValueError("申告理由が日本語agent contractの許可値にありません。")
    order = get_order_details(order_id)
    item = _find_item(order, item_id)
    fulfillment = get_fulfillment_status(order_id)
    tier = order["customer"]["loyalty_tier"]
    status = fulfillment["status"]

    if status in {"受付済み", "処理中"} and reason == "お客様都合":
        actions = ["キャンセル"]
        explanation = "発送前のためキャンセルできます。"
    elif status == "紛失":
        actions = ["代替品発送", "返金"]
        explanation = "配送中の紛失として代替品または返金を選べます。"
    elif reason in {"破損", "不良"}:
        actions = ["ストアクレジット"] if item["final_sale"] else ["返金", "交換"]
        explanation = "破損・不良品は期限と手数料の例外対象です。"
    elif reason == "遅配":
        actions = ["対象外"]
        explanation = "3日以上の遅配条件を満たしていません。"
    elif status != "配達済み":
        actions = ["対象外"]
        explanation = "現在の配送状況では指定された対応を行えません。"
    else:
        return_window = _return_window_days(tier, item["category"])
        if fulfillment["late_delivery"]:
            return_window += 15
        within_window = fulfillment["days_since_delivery"] <= return_window
        personal_care_ok = item["category"] != "パーソナルケア" or item["sealed"]
        if within_window and personal_care_ok and not item["final_sale"]:
            actions = ["交換", "返金"] if reason != "お客様都合" else ["返金"]
            explanation = "返品期限と商品条件を満たしています。"
        else:
            actions = ["対象外"]
            explanation = "返品期限、開封状態、または最終処分品の条件を満たしません。"

    if fulfillment["late_delivery"] and status == "配達済み":
        if actions == ["対象外"]:
            actions = ["配送遅延クレジット"]
            explanation = "約束日より3日以上遅れたためクレジット対象です。"
        elif "配送遅延クレジット" not in actions:
            actions.append("配送遅延クレジット")
            explanation += " 遅配クレジットも同時に適用できます。"

    restocking_fee_rate = 0.0
    if (
        "返金" in actions
        and item["category"] == "電子機器"
        and reason not in {"破損", "不良", "誤配送"}
        and status != "紛失"
    ):
        restocking_fee_rate = {
            "標準": 0.15,
            "ゴールド": 0.075,
            "プラチナ": 0.0,
        }[tier]

    result = {
        "order_id": order_id,
        "item_id": item_id,
        "eligible": actions != ["対象外"],
        "eligible_actions": actions,
        "reason": reason,
        "restocking_fee_rate": restocking_fee_rate,
        "explanation": explanation,
    }
    result["policy_id"] = _reference_id("POLICY", result)
    _remember(
        _POLICY_CHECKS,
        result["policy_id"],
        {
            "order_id": order_id,
            "item_id": item_id,
            "reason": reason,
            "result": result,
        },
    )
    return result


def check_inventory(sku: str) -> dict[str, Any]:
    details = _sku_details(sku)
    quantity = _stable_number(sku) % 6
    result = {
        "sku": sku,
        "product_name": details["name"],
        "unit_price_jpy": details["unit_price_jpy"],
        "available": quantity > 0,
        "quantity": quantity,
        "message": "交換可能です。" if quantity > 0 else "現在は在庫切れです。",
    }
    result["inventory_check_id"] = _reference_id("INVENTORY", result)
    _remember(
        _INVENTORY_CHECKS,
        result["inventory_check_id"],
        {"sku": sku, "result": result},
    )
    return result


def calculate_resolution(
    order_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not items:
        raise ValueError("対応する商品を1件以上指定してください。")
    order = get_order_details(order_id)
    tier = order["customer"]["loyalty_tier"]
    results: list[dict[str, Any]] = []
    total_refund = 0
    total_fee = 0
    total_credit = 0
    total_exchange_difference = 0
    seen_item_ids: set[str] = set()

    for requested in items:
        item = _find_item(order, requested["item_id"])
        item_id = item["item_id"]
        if item_id in seen_item_ids:
            raise ValueError("同じ商品IDを重複して指定できません。")
        seen_item_ids.add(item_id)

        actions = requested["actions"]
        reason = requested["reason"]
        if not actions or len(actions) != len(set(actions)):
            raise ValueError("対応内容は重複のない1件以上の配列で指定してください。")
        if any(action not in ACTIONS for action in actions):
            raise ValueError("対応内容が日本語agent contractの許可値にありません。")
        if reason not in REASONS:
            raise ValueError("申告理由が日本語agent contractの許可値にありません。")

        policy_check = _POLICY_CHECKS.get(requested["policy_id"])
        if not policy_check:
            raise ValueError("policy_idが未発行です。先にcheck_resolution_policyを実行してください。")
        if (
            policy_check["order_id"] != order_id
            or policy_check["item_id"] != item_id
            or policy_check["reason"] != reason
        ):
            raise ValueError("policy_idが現在の注文・商品・申告理由と一致しません。")
        policy = policy_check["result"]
        if not set(actions).issubset(policy["eligible_actions"]):
            raise ValueError("指定された対応はpolicyで許可されていません。")
        primary_actions = set(actions) - {"配送遅延クレジット"}
        if len(primary_actions) > 1:
            raise ValueError("返金、交換などの主要対応は1商品につき1つだけ指定してください。")

        amount = item["unit_price_jpy"] * item["quantity"]
        fee = 0
        refund = 0
        credit = 0
        exchange_difference = 0
        replacement_sku = requested.get("replacement_sku")
        inventory_check_id = requested.get("inventory_check_id")

        if "交換" in actions:
            if not replacement_sku or not inventory_check_id:
                raise ValueError("交換にはreplacement_skuとinventory_check_idが必要です。")
            inventory_check = _INVENTORY_CHECKS.get(inventory_check_id)
            if not inventory_check:
                raise ValueError(
                    "inventory_check_idが未発行です。先にcheck_inventoryを実行してください。"
                )
            if inventory_check["sku"] != replacement_sku:
                raise ValueError("inventory_check_idが交換先SKUと一致しません。")
            inventory = inventory_check["result"]
            if not inventory["available"]:
                raise ValueError("交換先SKUは在庫切れです。")
            current_sku = _sku_details(item["sku"])
            replacement = _sku_details(replacement_sku)
            if current_sku["product_index"] != replacement["product_index"]:
                raise ValueError("交換先SKUは同じ商品種別から選んでください。")
            exchange_difference = replacement["unit_price_jpy"] - amount
        elif replacement_sku or inventory_check_id:
            raise ValueError("交換以外では交換先SKUやinventory_check_idを指定できません。")

        if "返金" in actions or "キャンセル" in actions:
            refund = amount
            if (
                "返金" in actions
                and item["category"] == "電子機器"
                and reason not in {"破損", "不良", "誤配送"}
                and get_fulfillment_status(order_id)["status"] != "紛失"
            ):
                fee = round(
                    amount
                    * {"標準": 0.15, "ゴールド": 0.075, "プラチナ": 0.0}[tier]
                )
                refund -= fee
        elif "ストアクレジット" in actions:
            credit = amount
        if "配送遅延クレジット" in actions:
            credit += 1000

        total_refund += refund
        total_fee += fee
        total_credit += credit
        total_exchange_difference += exchange_difference
        results.append(
            {
                "item_id": item["item_id"],
                "product_name": item["name"],
                "actions": actions,
                "reason": reason,
                "replacement_sku": replacement_sku,
                "refund_jpy": refund,
                "restocking_fee_jpy": fee,
                "credit_jpy": credit,
                "exchange_difference_jpy": exchange_difference,
            }
        )

    result = {
        "order_id": order_id,
        "currency": "JPY",
        "items": results,
        "total_refund_jpy": total_refund,
        "total_restocking_fee_jpy": total_fee,
        "total_credit_jpy": total_credit,
        "total_exchange_difference_jpy": total_exchange_difference,
        "message": "解決内容の計算が完了しました。",
    }
    calculation_id = _reference_id("CALC", result)
    result["calculation_id"] = calculation_id
    _remember(
        _CALCULATIONS,
        calculation_id,
        {"order_id": order_id, "result": result},
    )
    return result


def submit_resolution(
    order_id: str,
    calculation_id: str,
    resolution_summary: str,
) -> dict[str, Any]:
    _order_number(order_id)
    calculation = _CALCULATIONS.get(calculation_id)
    if not calculation or calculation["order_id"] != order_id:
        raise ValueError("calculation_idがありません。先にcalculate_resolutionを実行してください。")
    if not resolution_summary.strip():
        raise ValueError("日本語の解決概要を指定してください。")
    digest = hashlib.sha256(
        f"{order_id}:{calculation_id}:{resolution_summary}".encode("utf-8")
    ).hexdigest()[:10].upper()
    return {
        "order_id": order_id,
        "case_id": f"CASE-{digest}",
        "calculation_id": calculation_id,
        "status": "処理シミュレーション完了",
        "resolution_summary": resolution_summary,
        "external_side_effect": False,
        "message": "架空の受付IDを発行しました。実システムは変更していません。",
    }

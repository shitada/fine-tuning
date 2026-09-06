"""日本語の架空問い合わせを固定し、承認済みの範囲だけHosted Agentへ送信する。

まず --dry-run --num-prompts 170 --seed 42 でmanifestを作成する。
送信には --send、--stage、agent名/version/project endpointの明示が必要。
応答本文・HTTPヘッダー・stderrは保存しない。失敗も結果不明も再送しない。
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit
import uuid


FIXTURES_DIR = Path(__file__).resolve().parent
DEMO_DIR = FIXTURES_DIR.parent
AGENT_ROOT = DEMO_DIR / "agent"
LOCAL_AZD_CONFIG = AGENT_ROOT / ".azd-client.json"
SOURCE_DIR = AGENT_ROOT / "src" / "zava-traces-demo"
sys.path.insert(0, str(SOURCE_DIR))

from synthetic_store import (  # noqa: E402
    ACTIONS,
    REASONS,
    REFERENCE_DATE,
    TIERS,
    build_order_id,
    check_inventory,
    check_resolution_policy,
    get_fulfillment_status,
    get_order_details,
)


SCHEMA_VERSION = 1
AZD_SETUP_TIMEOUT_SECONDS = 240
CATEGORY_COUNTS = {
    "返品": 35,
    "交換": 30,
    "配送問題": 30,
    "キャンセル": 20,
    "複数商品": 25,
    "曖昧・方針質問": 30,
}
SMOKE = ("返品", "交換", "配送問題", "配送問題",
         "配送問題", "キャンセル", "複数商品", "曖昧・方針質問")
ERROR_KINDS = {
    "コマンド失敗", "HTTPエラー", "HTTP形式不正", "JSON不正",
    "応答形式不正", "応答エラー", "応答失敗", "応答未完了",
    "タイムアウト・結果不明", "中断・結果不明", "実行失敗・結果不明",
    "UTF-8不正・結果不明", "結果未確認",
}
JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
SAFE_RESPONSE_ID = re.compile(r"(?:caresp|resp)_[A-Za-z0-9_-]{1,128}")
SAFE_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])(?:ORD-\d{3,6}|ITEM-\d{3,6}-\d+|SKU-\d{3}-[1-3]|"
    r"(?:POLICY|INVENTORY|CALC)-[A-F0-9]{12}|CASE-[A-F0-9]{10}|"
    r"(?:caresp|resp)_[A-Za-z0-9_-]+)(?![A-Za-z0-9_])"
)


class TrafficError(Exception):
    """本文や資格情報を含まない、表示可能な検証エラー。"""


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def contract_hashes() -> dict[str, str]:
    hashes = {}
    for name in ("zava_system_prompt.md", "zava_tools.json"):
        approved = (FIXTURES_DIR / name).read_bytes()
        if approved != (SOURCE_DIR / "contract" / name).read_bytes():
            raise TrafficError("承認済み仕様とagent同梱仕様が一致しません。")
        hashes[name] = digest(approved)
    hashes["synthetic_store.py"] = digest((SOURCE_DIR / "synthetic_store.py").read_bytes())
    hashes["共通語彙"] = digest(json_text({
        "reasons": sorted(REASONS), "actions": sorted(ACTIONS), "tiers": TIERS,
    }).encode("utf-8"))
    return hashes


def allocate(count: int, weights: dict[str, int]) -> dict[str, int]:
    """最大剰余法。並び順を同点時の優先順位とする。"""
    total = sum(weights.values())
    if count == 0:
        return dict.fromkeys(weights, 0)
    result = {key: count * weight // total for key, weight in weights.items()}
    ranked = sorted(weights, key=lambda key: -(count * weights[key] % total))
    for key in ranked[:count - sum(result.values())]:
        result[key] += 1
    return result


def category_schedule(count: int, rng: random.Random) -> list[str]:
    if not 8 <= count <= 200:
        raise TrafficError("scenario-countは8～200件で指定してください。")
    smoke_counts = Counter(SMOKE)
    remaining = allocate(count - 8, {
        key: value - smoke_counts[key] for key, value in CATEGORY_COUNTS.items()
    })
    pilot = allocate(min(25, count - 8), remaining)
    schedule = list(SMOKE)
    # Pilotはcategoryを交互に配置し、先頭に同じ種類が偏らないようにする。
    for index in range(max(pilot.values(), default=0)):
        schedule.extend(key for key in CATEGORY_COUNTS if pilot[key] > index)
    rest = [key for key in CATEGORY_COUNTS for _ in range(remaining[key] - pilot[key])]
    rng.shuffle(rest)
    return schedule + rest


def generate_scenarios(seed: int = 42, count: int = 170,
                       pool_limit: int = 4096) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    schedule = category_schedule(count, rng)
    pool = []
    for number in range(1, pool_limit + 1):
        order_id = build_order_id(number)
        pool.append((get_order_details(order_id), get_fulfillment_status(order_id)))
    rng.shuffle(pool)
    skus = sorted({item["sku"] for order, _ in pool for item in order["items"]})
    inventory = [check_inventory(sku) for sku in skus]
    used: set[str] = set()
    category_index: Counter[str] = Counter()
    scenarios = []

    def alternatives(item: dict, available: bool = True) -> list[dict]:
        return [
            stock for stock in inventory
            if stock["product_name"] == item["name"]
            and stock["sku"] != item["sku"] and stock["available"] is available
        ]

    def choose(predicate):
        for order, shipping in pool:
            if order["order_id"] in used:
                continue
            for item in order["items"]:
                if predicate(order, shipping, item):
                    used.add(order["order_id"])
                    return order, shipping, item
        raise TrafficError("条件に合う未使用注文の有限候補が尽きました。送信は行いません。")

    def delivered(order, shipping, item):
        return shipping["status"] == "配達済み"

    def policy(order, item, reason="お客様都合"):
        return check_resolution_policy(order["order_id"], item["item_id"], reason)

    def label(item):
        return f"「{item['name']}」（商品ID {item['item_id']}、SKU {item['sku']}）"

    for index, category in enumerate(schedule):
        variant_index = category_index[category]
        category_index[category] += 1
        replacements = []
        reports = []
        if category == "返品":
            variant = (
                "通常返品", "期限の確認", "最終処分品", "開封済み",
                "電子機器手数料", "破損例外", "最終処分品の不良", "遅配後の返品",
            )[variant_index % 8]

            def matches(order, shipping, item):
                if not delivered(order, shipping, item):
                    return False
                if variant == "通常返品":
                    return "返金" in policy(order, item)["eligible_actions"]
                if variant == "期限の確認":
                    return (not item["final_sale"] and item["sealed"]
                            and not shipping["late_delivery"]
                            and policy(order, item)["eligible_actions"] == ["対象外"])
                if variant in {"最終処分品", "最終処分品の不良"}:
                    return item["final_sale"]
                if variant == "開封済み":
                    return item["category"] == "パーソナルケア" and not item["sealed"]
                if variant == "電子機器手数料":
                    return policy(order, item)["restocking_fee_rate"] > 0
                if variant == "破損例外":
                    return not item["final_sale"] and shipping["days_since_delivery"] > 45
                return shipping["late_delivery"]

            order, shipping, item = choose(matches)
            reason = {"破損例外": "破損", "最終処分品の不良": "不良"}.get(
                variant, "お客様都合")
            complaint = {
                "破損": "商品に破損があるため",
                "不良": "商品に不良があるため",
                "お客様都合": "使う予定がなくなったため",
            }[reason]
            request = (f"{label(item)}を{complaint}返品したいです。"
                       "返金できるか、必要な手数料も含めて確認してください。")
            if variant == "開封済み":
                request += "開封済みです。"
            if item["final_sale"]:
                request += "最終処分品という表示があります。"
            if variant in {"期限の確認", "破損例外", "遅配後の返品"}:
                request += f"配達から{shipping['days_since_delivery']}日経っています。"
            reports = [{"item_id": item["item_id"], "reason": reason}]
            items = [item]
        elif category == "交換":
            variant = ("不良交換", "サイズ違い", "色違い", "最終処分品", "在庫確認")[
                variant_index % 5]
            available = variant != "在庫確認"

            def matches(order, shipping, item):
                return (
                    delivered(order, shipping, item)
                    and bool(alternatives(item, available))
                    and (variant != "サイズ違い" or item["category"] == "衣料品")
                    and (variant != "最終処分品" or item["final_sale"])
                )

            order, shipping, item = choose(matches)
            target = rng.choice(alternatives(item, available))
            reason = variant if variant in {"サイズ違い", "色違い"} else "不良"
            complaint = {
                "サイズ違い": "試してみるとサイズが合いませんでした",
                "色違い": "希望していた色とは違いました",
                "不良": "商品に不良があります",
            }[reason]
            request = (
                f"{label(item)}について、{complaint}。"
                f"交換先は「{target['product_name']}」のSKU {target['sku']}を希望します。"
                "交換の可否と在庫、差額を確認してください。"
            )
            reports = [{"item_id": item["item_id"], "reason": reason}]
            replacements = [{"item_id": item["item_id"], "sku": target["sku"],
                             "name": target["product_name"], "available": target["available"]}]
            items = [item]
        elif category == "配送問題":
            variant = ("紛失", "遅配", "発送済み")[variant_index % 3]
            order, shipping, item = choose(
                lambda o, s, i: (s["late_delivery"] if variant == "遅配"
                                  else s["status"] == variant))
            if variant == "紛失":
                preference = "代替品の発送" if variant_index % 2 == 0 else "返金"
                request = (f"{label(item)}が届かず、配送状況は紛失になっています。"
                           f"{preference}を希望しています。対応を確認してください。")
                reason = "未着"
            elif variant == "遅配":
                request = (
                    f"{label(item)}は、約束日{shipping['promised_date']}に対して"
                    f"{shipping['delivery_date']}に届き、{shipping['days_late']}日遅れました。"
                    "遅配に対する補償と返品期限への影響を教えてください。"
                )
                reason = "遅配"
            else:
                request = (f"{label(item)}は発送済みですが、まだ受け取っていません。"
                           f"お届け予定日は{shipping['promised_date']}と表示されています。"
                           "配送状況を確認し、今できる対応を教えてください。")
                reason = "未着"
            items = [item]
            reports = [{"item_id": item["item_id"], "reason": reason}]
        elif category == "キャンセル":
            variant = ("受付済み", "処理中", "発送済み", "配達済み")[variant_index % 4]
            order, shipping, item = choose(lambda o, s, i: s["status"] == variant)
            items = order["items"]
            request = ("、".join(label(entry) for entry in items)
                       + "について、都合が変わり不要になりました。"
                       "この注文全体をキャンセルできるか確認してください。"
                       "できない場合は、その理由と選べる対応を教えてください。")
            reports = [{"item_id": entry["item_id"], "reason": "お客様都合"} for entry in items]
        elif category == "複数商品":
            variant = ("一部返品", "両方破損", "返品と交換", "遅配と返品", "条件別相談")[
                variant_index % 5]
            order, shipping, item = choose(
                lambda o, s, i: len(o["items"]) == 2 and delivered(o, s, i)
                and (variant != "遅配と返品" or s["late_delivery"])
                and (variant != "返品と交換" or bool(alternatives(o["items"][1]))))
            items = order["items"]
            first, second = items
            if variant == "一部返品":
                request = (f"{label(first)}だけ不要になったので返品したいです。"
                           f"{label(second)}は手元に残します。条件と金額を確認してください。")
                reports = [{"item_id": first["item_id"], "reason": "お客様都合"}]
            elif variant == "両方破損":
                request = (f"{label(first)}と{label(second)}の両方に破損があります。"
                           "商品ごとに返品や返金が可能か確認してください。")
                reports = [{"item_id": entry["item_id"], "reason": "破損"} for entry in items]
            elif variant == "返品と交換":
                target = rng.choice(alternatives(second))
                request = (
                    f"{label(first)}は不要になったので返品を希望します。"
                    f"{label(second)}は不良があるので、"
                    f"「{target['product_name']}」のSKU {target['sku']}に交換したいです。"
                    "それぞれの条件と金額を確認してください。"
                )
                reports = [{"item_id": first["item_id"], "reason": "お客様都合"},
                           {"item_id": second["item_id"], "reason": "不良"}]
                replacements = [{"item_id": second["item_id"], "sku": target["sku"],
                                 "name": target["product_name"], "available": target["available"]}]
            else:
                request = (f"{label(first)}と{label(second)}が不要になりました。"
                           "商品ごとの返品条件と手数料を確認してください。")
                if variant == "遅配と返品":
                    request += f"どちらも予定より{shipping['days_late']}日遅れて届いています。補償も確認したいです。"
                reports = [{"item_id": entry["item_id"], "reason": "お客様都合"} for entry in items]
        else:
            variant = ("理由未指定", "返品期限", "手数料", "最終処分品", "開封条件", "会員条件")[
                variant_index % 6]
            order, shipping, item = choose(
                lambda o, s, i: (
                    variant == "理由未指定" or delivered(o, s, i))
                and (variant != "手数料" or i["category"] == "電子機器")
                and (variant != "最終処分品" or i["final_sale"])
                and (variant != "開封条件" or i["category"] == "パーソナルケア"))
            question = {
                "理由未指定": "少し困っています。どんな情報を伝えれば相談できますか。",
                "返品期限": "返品期限を知りたいです。いつまで相談できますか。",
                "手数料": "自己都合で返品する場合、手数料はどのように決まりますか。",
                "最終処分品": "最終処分品と表示されています。返品の条件や例外を教えてください。",
                "開封条件": "返品する場合、開封状態によって条件は変わりますか。",
                "会員条件": f"会員区分は{order['customer']['loyalty_tier']}です。返品条件を教えてください。",
            }[variant]
            request = f"{label(item)}について、{question}まだ処理は確定しないでください。"
            items = [item]

        prompt = (
            f"この架空の問い合わせは{REFERENCE_DATE.isoformat()}時点のものです。"
            f"注文ID {order['order_id']}の相談です。"
            f"注文画面の配送状況は「{shipping['status']}」です。{request}"
        )
        scenarios.append({
            "schema_version": SCHEMA_VERSION, "scenario_id": f"SCN-{index + 1:04d}",
            "index": index, "seed": seed, "reference_date": REFERENCE_DATE.isoformat(),
            "category": category, "variant": variant, "order_id": order["order_id"],
            "loyalty_tier": order["customer"]["loyalty_tier"],
            "items": items, "fulfillment": shipping, "reported_reasons": reports,
            "replacements": replacements, "prompt": prompt,
        })
    validate_scenarios(scenarios)
    return scenarios


def validate_scenarios(scenarios: list[dict]) -> dict:
    seen_orders, seen_prompts = set(), set()
    for index, scenario in enumerate(scenarios):
        order = get_order_details(scenario["order_id"])
        shipping = get_fulfillment_status(scenario["order_id"])
        if (scenario["order_id"] in seen_orders or scenario["prompt"] in seen_prompts
                or scenario["index"] != index or scenario["scenario_id"] != f"SCN-{index + 1:04d}"):
            raise TrafficError("注文、文章、またはscenario IDが重複・不整合です。")
        seen_orders.add(scenario["order_id"])
        seen_prompts.add(scenario["prompt"])
        if (scenario["fulfillment"] != shipping
                or scenario["loyalty_tier"] != order["customer"]["loyalty_tier"]
                or scenario["reference_date"] != REFERENCE_DATE.isoformat()):
            raise TrafficError("注文の配送状況、会員区分、基準日が不整合です。")
        items = scenario["items"]
        if not items or len({item["item_id"] for item in items}) != len(items):
            raise TrafficError("対象商品が空または重複しています。")
        for item in items:
            if item not in order["items"] or any(
                str(item[key]) not in scenario["prompt"] for key in ("name", "item_id", "sku")
            ):
                raise TrafficError("商品が実際の架空注文または文章と一致しません。")
        for target in scenario["replacements"]:
            source = next((item for item in items if item["item_id"] == target["item_id"]), None)
            stock = check_inventory(target["sku"])
            if (source is None or source["sku"] == target["sku"]
                    or target["name"] != stock["product_name"]
                    or target["available"] != stock["available"]
                    or target["sku"] not in scenario["prompt"]):
                raise TrafficError("交換先SKUが注文・在庫・文章と一致しません。")
        for report in scenario["reported_reasons"]:
            if report["reason"] not in REASONS or report["item_id"] not in {
                item["item_id"] for item in items
            }:
                raise TrafficError("申告理由または対象商品が仕様と一致しません。")
    return {"duplicate_orders": 0, "duplicate_prompts": 0,
            "order_item_integrity": True, "fulfillment_integrity": True,
            "replacement_integrity": True}


@lru_cache(maxsize=1)
def allowed_english_tokens() -> frozenset[str]:
    schema = json.loads((FIXTURES_DIR / "zava_tools.json").read_text(encoding="utf-8"))
    allowed = {
        "Zava", "SKU", "JPY", "ID", "JSON", "true", "false", "null",
        "id", "type", "role", "content", "text", "output", "output_text",
        "input_tokens", "output_tokens", "response_id",
    }

    def collect(value):
        if isinstance(value, dict):
            allowed.update(value.get("properties", {}).keys())
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(schema)
    allowed.update(tool["function"]["name"] for tool in schema)
    # Tool結果のproperty名も実装から取得する。任意のbacktick文やsnake_caseは除外しない。
    source = ast.parse((SOURCE_DIR / "synthetic_store.py").read_text(encoding="utf-8"))
    for node in ast.walk(source):
        if isinstance(node, ast.Dict):
            allowed.update(
                key.value for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
                and re.fullmatch(r"[a-z][a-z0-9_]*", key.value))
    return frozenset(allowed)


def language_metrics(response: dict) -> dict:
    output = response.get("output")
    texts = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("role") != "assistant":
                continue
            content = item.get("content")
            if isinstance(content, list):
                texts.extend(part["text"] for part in content if isinstance(part, dict)
                             and part.get("type") in {"output_text", "text"}
                             and isinstance(part.get("text"), str))
    text = "\n".join(texts)
    if not text:
        return {"japanese_script": None, "replacement_character": None,
                "unapproved_english": None, "unapproved_english_token_count": None,
                "business_refusal_hint": None}
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]*", SAFE_IDENTIFIER.sub("", text))
    english_count = sum(word not in allowed_english_tokens() for word in words)
    return {
        "japanese_script": bool(JAPANESE.search(text)),
        "replacement_character": "\ufffd" in text,
        "unapproved_english": english_count > 0,
        "unapproved_english_token_count": english_count,
        "business_refusal_hint": any(word in text for word in (
            "対象外", "キャンセルできません", "返品できません", "交換できません", "返金できません")),
    }


def empty_result(error_kind: str, http_status: int | None = None) -> dict:
    return {
        "success": False, "error_kind": error_kind, "http_status": http_status,
        "exit_code": None, "diagnostic_code": None,
        "response_id": None, "input_tokens": None, "output_tokens": None,
        "response_visible_tool_call_count": None, "business_outcome": None,
        "japanese_script": None, "replacement_character": None,
        "unapproved_english": None, "unapproved_english_token_count": None,
        "business_refusal_hint": None,
    }


def command_diagnostic(stderr: str) -> str:
    aad = re.search(r"\bAADSTS\d{3,10}\b", stderr)
    if aad:
        return aad.group()
    if re.search(r"\brpc error:\s*code\s*=\s*DeadlineExceeded\b", stderr, re.IGNORECASE):
        return "rpc_timeout"
    for code, pattern in (
        ("rate_limit", r"too many requests|rate.?limit|quota exceeded"),
        ("authentication", r"unauthorized|authentication failed|not logged in|please.*log.?in|"
         r"AzureDeveloperCLICredential|CredentialUnavailable|failed to acquire.*token"),
        ("authorization", r"forbidden|authorizationfailed|permission denied"),
        ("tls", r"tls|certificate.*(?:failed|invalid)|ssl"),
        ("timeout", r"timed? out|deadline exceeded"),
        ("network", r"connection (?:reset|refused)|no such host|name resolution"),
        ("session_not_ready", r"session_not_ready|faileddependency"),
    ):
        if re.search(pattern, stderr, re.IGNORECASE):
            return code
    return "unclassified_command_failure"


def parse_raw_response(stdout: str, returncode: int, stderr: str = "") -> dict:
    """HTTP+SSE/JSONを厳密に読む。本文、header、error文字列を戻り値に含めない。"""
    raw = stdout.replace("\r\n", "\n").lstrip("\ufeff")
    status_match = re.match(r"(?:HTTP(?:/\d(?:\.\d)?)?|Status:)\s+(\d{3})[^\n]*\n", raw)
    status = int(status_match[1]) if status_match else None
    if returncode != 0:
        diagnostic_text = stderr + "\n" + stdout
        if status is None:
            error_status = re.search(
                r"(?:HTTP(?:/\d(?:\.\d)?)?|status(?:\s*code)?)\s*[:=]?\s*([45]\d{2})\b",
                diagnostic_text, re.IGNORECASE,
            )
            status = int(error_status[1]) if error_status else None
        return {
            **empty_result("コマンド失敗", status),
            "exit_code": returncode,
            "diagnostic_code": command_diagnostic(diagnostic_text),
        }
    if status is None:
        return empty_result("HTTP形式不正")
    if not 200 <= status < 300:
        return empty_result("HTTPエラー", status)
    split = raw.find("\n\n")
    if split < 0:
        return empty_result("HTTP形式不正", status)
    body = raw[split + 2:].strip()
    completed = None
    try:
        if body.startswith("{"):
            payload = json.loads(body)
            if not isinstance(payload, dict):
                return empty_result("応答形式不正", status)
            if payload.get("type") == "response.completed":
                completed = payload.get("response")
            elif payload.get("type") in {"error", "response.error"} or payload.get("error") is not None:
                return empty_result("応答エラー", status)
            elif payload.get("type") == "response.failed":
                return empty_result("応答失敗", status)
            else:
                completed = payload
        else:
            for block in re.split(r"\n\s*\n", body):
                event = ""
                data = []
                for line in block.splitlines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif line and not line.startswith((":", "id:", "retry:")):
                        return empty_result("応答形式不正", status)
                if not data:
                    continue
                encoded = "\n".join(data)
                if encoded == "[DONE]":
                    continue
                payload = json.loads(encoded)
                if not isinstance(payload, dict):
                    return empty_result("応答形式不正", status)
                kind = payload.get("type", event)
                if event and kind != event:
                    return empty_result("応答形式不正", status)
                if kind in {"error", "response.error"} or payload.get("error") is not None:
                    return empty_result("応答エラー", status)
                if kind == "response.failed":
                    return empty_result("応答失敗", status)
                if kind == "response.incomplete":
                    return empty_result("応答未完了", status)
                if kind == "response.completed":
                    if completed is not None:
                        return empty_result("応答形式不正", status)
                    completed = payload.get("response")
    except (json.JSONDecodeError, TypeError, ValueError):
        return empty_result("JSON不正", status)
    if not isinstance(completed, dict):
        return empty_result("応答未完了", status)
    if completed.get("error") is not None:
        return empty_result("応答エラー", status)
    if completed.get("status") == "failed":
        return empty_result("応答失敗", status)
    if completed.get("status") != "completed":
        return empty_result("応答未完了", status)
    result = empty_result("応答未完了", status)
    result.update(success=True, error_kind=None)
    response_id = completed.get("id")
    if isinstance(response_id, str) and SAFE_RESPONSE_ID.fullmatch(response_id):
        result["response_id"] = response_id
    usage = completed.get("usage")
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                result[key] = value
    output = completed.get("output")
    if isinstance(output, list):
        result["response_visible_tool_call_count"] = sum(
            isinstance(item, dict) and item.get("type") == "function_call" for item in output)
    result.update(language_metrics(completed))
    return result


def configured_endpoint(agent_root: Path = AGENT_ROOT) -> str:
    # このprojectの単一、リテラルendpointだけを扱う。一般的なYAML解釈はしない。
    text = (agent_root / "azure.yaml").read_text(encoding="utf-8")
    endpoints = re.findall(r"^\s+endpoint:\s*(https://[^\s#]+)\s*$", text, re.MULTILINE)
    if len(endpoints) != 1:
        raise TrafficError("azure.yamlの単一のリテラルproject endpointを確認できません。")
    return normalize_endpoint(endpoints[0])


def normalize_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.port or not parsed.hostname
            or not re.fullmatch(r"/api/projects/[A-Za-z0-9_-]+/?", parsed.path)):
        raise TrafficError("project endpointの形式が不正です。")
    return endpoint.rstrip("/")


def azd_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["AZURE_DEV_USER_AGENT"] = "microsoft_foundry_skill"
    profile = env.get("AZD_CONFIG_DIR")
    if profile is None and LOCAL_AZD_CONFIG.exists():
        try:
            config = json.loads(LOCAL_AZD_CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise TrafficError("ローカルazd設定を読み取れません。送信前に設定を確認してください。") from None
        if not isinstance(config, dict) or set(config) != {"config_dir"}:
            raise TrafficError("ローカルazd設定にはconfig_dirだけを指定してください。")
        profile = config["config_dir"]
        if not isinstance(profile, str):
            raise TrafficError("azdのconfig_dirは絶対パスの文字列で指定してください。")
    if profile is not None:
        if (not profile or not Path(profile).is_absolute()
                or not Path(profile).is_dir() or not (Path(profile) / "config.json").is_file()):
            raise TrafficError("指定されたazdプロファイルが存在しないか、初期化されていません。")
        env["AZD_CONFIG_DIR"] = profile
    return env


def invoke(scenario: dict, agent_name: str, agent_version: str, timeout: int,
           *, project_endpoint: str | None = None) -> dict:
    project_endpoint = normalize_endpoint(
        configured_endpoint() if project_endpoint is None else project_endpoint
    )
    endpoint = (
        f"{project_endpoint}/agents/{agent_name}"
        "/endpoint/protocols/openai/responses?api-version=v1"
    )
    command = [
        "azd", "ai", "agent", "invoke", scenario["prompt"],
        "--agent-endpoint", endpoint,
        "--version", agent_version, "--new-conversation", "--output", "raw",
        "--timeout", str(timeout), "--no-prompt",
    ]
    env = azd_environment()
    try:
        process = subprocess.run(
            command, cwd=AGENT_ROOT, env=env, shell=False, capture_output=True,
            # azd's HTTP timeout excludes CLI startup and its credential acquisitions.
            encoding="utf-8", errors="strict",
            timeout=timeout + AZD_SETUP_TIMEOUT_SECONDS, check=False,
        )
        return parse_raw_response(process.stdout, process.returncode, process.stderr)
    except subprocess.TimeoutExpired:
        return empty_result("タイムアウト・結果不明")
    except UnicodeError:
        return empty_result("UTF-8不正・結果不明")
    except KeyboardInterrupt:
        return empty_result("中断・結果不明")
    except OSError:
        return empty_result("実行失敗・結果不明")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path: Path, value: dict) -> None:
    writing = path.with_name(path.name + ".writing")
    with writing.open("w", encoding="utf-8", errors="strict", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    # OneDrive等による短いfile lockだけ再試行する。外部呼び出しは再試行しない。
    for attempt in range(6):
        try:
            os.replace(writing, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * 2 ** attempt)


def append_event(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8", errors="strict", newline="\n") as handle:
        handle.write(json_text(event) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def run_lock(run_dir: Path):
    run_dir.mkdir(parents=True, exist_ok=True)
    lock = run_dir / "traffic.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise TrafficError("実行ロックがあります。並行実行は禁止です。中断時は記録とプロセスを診断してください。") from None
    try:
        with handle:
            handle.write(str(os.getpid()))
            handle.flush()
            yield
    finally:
        lock.unlink()


def read_events(path: Path, scenarios: list[dict], manifest_hash: str) -> tuple[list, dict]:
    stages: dict[str, dict] = {}
    attempts: dict[str, dict] = {}
    if not path.exists():
        return [], stages
    try:
        for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
            event = json.loads(line)
            if event["manifest_sha256"] != manifest_hash:
                raise TrafficError("別manifestの送信記録が存在します。上書きできません。")
            kind = event["event"]
            if kind == "stage_started":
                stage = event["stage"]
                offset, count = event["offset"], event["count"]
                if (stage in stages or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", stage)
                        or type(offset) is not int or type(count) is not int
                        or offset < 0 or count < 1 or offset + count > len(scenarios)):
                    raise TrafficError("段階の記録が重複または不正です。")
                stages[stage] = event
            elif kind in {"submitted", "completed"}:
                scenario = scenarios[event["index"]]
                stage = stages[event["stage"]]
                if (event["scenario_id"] != scenario["scenario_id"]
                        or event["category"] != scenario["category"]
                        or not stage["offset"] <= event["index"] < stage["offset"] + stage["count"]):
                    raise TrafficError("送信記録とscenarioが一致しません。")
                key = scenario["scenario_id"]
                if kind == "submitted":
                    if key in attempts:
                        raise TrafficError("再送されたscenarioの記録があります。")
                    attempts[key] = event
                else:
                    prior = attempts[key]
                    if (prior["event"] != "submitted"
                            or prior["request_id"] != event["request_id"]
                            or prior["stage"] != event["stage"]
                            or prior["started_at_utc"] != event["started_at_utc"]
                            or type(event["success"]) is not bool
                            or event["error_kind"] not in ERROR_KINDS | {None}):
                        raise TrafficError("完了記録が送信記録と一致しません。")
                    attempts[key] = event
            else:
                raise TrafficError("送信記録に未知のイベントがあります。")
    except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
        raise TrafficError("送信記録が不完全または不正です。自動修復・再送は行いません。") from None
    return list(attempts.values()), stages


def aggregate(scenarios: list[dict], attempts: list[dict]) -> dict:
    submitted = {attempt["scenario_id"] for attempt in attempts}
    finished = [attempt for attempt in attempts if attempt["event"] == "completed"]
    succeeded = [attempt for attempt in finished if attempt["success"]]
    unknown = [attempt for attempt in attempts if attempt["event"] == "submitted"
               or "結果不明" in (attempt.get("error_kind") or "")
               or (attempt.get("error_kind") == "コマンド失敗"
                   and attempt.get("http_status") is None)]
    usage = {}
    for key in ("input_tokens", "output_tokens", "response_visible_tool_call_count"):
        values = [attempt[key] for attempt in attempts if attempt.get(key) is not None]
        usage[key] = {
            "known_sum": sum(values) if values else None,
            "known_mean": sum(values) / len(values) if values else None,
            "known_count": len(values), "null_count": len(attempts) - len(values),
        }
    categories = {}
    for category in CATEGORY_COUNTS:
        selected = [scenario for scenario in scenarios if scenario["category"] == category]
        records = [attempt for attempt in attempts if attempt["category"] == category]
        categories[category] = {
            "count": len(selected), "submitted": len(records),
            "successes": sum(attempt["event"] == "completed" and attempt["success"] for attempt in records),
            "failures": sum(attempt["event"] == "completed" and not attempt["success"] for attempt in records),
            "unsubmitted": sum(scenario["scenario_id"] not in submitted for scenario in selected),
            "pending_unknown": sum(attempt["event"] == "submitted" for attempt in records),
        }
    language = {}
    for key in ("japanese_script", "replacement_character", "unapproved_english", "business_refusal_hint"):
        values = [attempt[key] for attempt in attempts if attempt.get(key) is not None]
        language[key] = {
            "true_count": sum(values), "known_count": len(values),
            "null_count": len(attempts) - len(values),
            "rate_known": sum(values) / len(values) if values else None,
        }
    return {
        "count": len(scenarios), "submitted_count": len(attempts),
        "completed_count": len(finished), "success_count": len(succeeded),
        "failure_count": len(finished) - len(succeeded), "unknown_outcome_count": len(unknown),
        "unsubmitted_count": len(scenarios) - len(attempts),
        "start_utc": min((a["started_at_utc"] for a in attempts), default=None),
        "last_attempt_end_utc": max((a["ended_at_utc"] for a in finished), default=None),
        "last_success_utc": max((a["ended_at_utc"] for a in succeeded), default=None),
        "response_level_usage": usage, "language_metrics": language,
        "categories": categories,
    }


def prepare_manifest(run_dir: Path, scenarios: list[dict], seed: int) -> dict:
    manifest = run_dir / "traffic-scenarios.jsonl"
    summary_path = run_dir / "traffic-summary.json"
    contents = "".join(json_text(scenario) + "\n" for scenario in scenarios).encode("utf-8")
    generation = {
        "schema_version": SCHEMA_VERSION, "seed": seed, "count": len(scenarios),
        "reference_date": REFERENCE_DATE.isoformat(), "manifest_sha256": digest(contents),
        "contract_hashes": contract_hashes(),
        "category_counts": dict(Counter(scenario["category"] for scenario in scenarios)),
        "integrity": validate_scenarios(scenarios),
    }
    previous = {}
    if not summary_path.exists() and (run_dir / "traffic-requests.jsonl").exists():
        raise TrafficError("送信記録の仕様hashを保持するsummaryがありません。診断が必要です。")
    if summary_path.exists():
        try:
            previous = json.loads(summary_path.read_text(encoding="utf-8", errors="strict"))
        except (ValueError, UnicodeError):
            raise TrafficError("既存summaryが不正です。上書きしません。") from None
        if not isinstance(previous, dict) or previous.get("generation") != generation:
            raise TrafficError("既存summaryの生成条件・仕様hashが異なります。上書きしません。")
    if manifest.exists():
        if manifest.read_bytes() != contents:
            raise TrafficError("既存manifestが異なります。上書きしません。")
    else:
        if summary_path.exists() or (run_dir / "traffic-requests.jsonl").exists():
            raise TrafficError("送信記録またはsummaryに対応するmanifestがありません。")
        with manifest.open("xb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    previous["generation"] = generation
    return previous


def update_summary(run_dir: Path, scenarios: list[dict], summary: dict) -> tuple[list, dict]:
    attempts, stages = read_events(
        run_dir / "traffic-requests.jsonl", scenarios, summary["generation"]["manifest_sha256"])
    cumulative = aggregate(scenarios, attempts)
    previous = summary.get("cumulative", {})
    if (any(cumulative[key] < previous.get(key, 0)
            for key in ("submitted_count", "completed_count", "success_count", "failure_count"))
            or not set(summary.get("stages", {})).issubset(stages)):
        raise TrafficError("保存済みsummaryより送信記録が減っています。記録の消失を診断し、再送しないでください。")
    summary["cumulative"] = cumulative
    summary["stages"] = {}
    for name, definition in stages.items():
        offset, count = definition["offset"], definition["count"]
        records = [attempt for attempt in attempts if attempt["stage"] == name]
        stats = aggregate(scenarios[offset:offset + count], records)
        stats.update(offset=offset, requested_count=count, target=definition["target"])
        stats["status"] = (
            "失敗停止・要診断" if stats["failure_count"] else
            "結果不明・要診断" if stats["unknown_outcome_count"] else
            "完了" if stats["success_count"] == count else "未送信あり")
        summary["stages"][name] = stats
    summary.setdefault("appinsights_cumulative_billing_usage", None)
    summary["notes"] = [
        "successはAPI応答の完了を意味し、返金・交換等の業務承認とは区別する。",
        "business_outcomeは未評価。拒否表現の検出は補助指標であり業務判定ではない。",
        "応答単位usageは課金総量ではない。累積推論usage・内部tool数はApp Insightsで別途確認する。",
        "nullは未取得。0 tokenや無料を意味しない。既知分の合計・平均は欠落分を含まない。",
        "日本語文字・英語allowlistは補助検査。日本語品質とtrace本文は別途確認する。",
        "UTC開始は最初の送信直前。trace終了windowは最後の成功から90秒以上後に別途確定する。",
        "送信済み・失敗・結果不明は自動再送しない。失敗後の未送信分は診断後に新しい段階名で明示する。",
    ]
    write_json(run_dir / "traffic-summary.json", summary)
    return attempts, stages


def send_stage(run_dir: Path, scenarios: list[dict], summary: dict, *, offset: int,
               count: int, stage: str, target: dict, timeout: int) -> int:
    attempts, stages = update_summary(run_dir, scenarios, summary)
    batch = scenarios[offset:offset + count]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", stage):
        raise TrafficError("stageは40文字以内の英数字、ハイフン、underscoreで指定してください。")
    if offset < 0 or count < 1 or len(batch) != count:
        raise TrafficError("送信範囲がmanifestの範囲外です。")
    if stage in stages:
        raise TrafficError("この段階名は使用済みです。再送は行いません。")
    if any(definition["target"] != target for definition in stages.values()):
        raise TrafficError("既存送信記録のagent/version/endpointと一致しません。")
    submitted = {attempt["scenario_id"] for attempt in attempts}
    if any(scenario["scenario_id"] in submitted for scenario in batch):
        raise TrafficError("送信範囲に送信済みまたは結果不明のscenarioがあります。再送は禁止です。")
    journal = run_dir / "traffic-requests.jsonl"
    manifest_hash = summary["generation"]["manifest_sha256"]
    append_event(journal, {
        "event": "stage_started", "stage": stage, "offset": offset, "count": count,
        "target": target, "manifest_sha256": manifest_hash,
    })
    update_summary(run_dir, scenarios, summary)
    for scenario in batch:
        # Write-ahead記録をfsyncしてから呼ぶ。強制終了しても同じscenarioは再送しない。
        attempt = {
            **empty_result("結果未確認"), "event": "submitted", "stage": stage,
            "manifest_sha256": manifest_hash, "request_id": str(uuid.uuid4()),
            "scenario_id": scenario["scenario_id"], "index": scenario["index"],
            "category": scenario["category"], "started_at_utc": utc_now(),
            "ended_at_utc": None, "duration_seconds": None,
        }
        started = time.monotonic()
        append_event(journal, attempt)
        try:
            result = invoke(
                scenario, target["agent_name"], target["agent_version"], timeout,
                project_endpoint=target["project_endpoint"],
            )
        except KeyboardInterrupt:
            result = empty_result("中断・結果不明")
        completed = {
            **attempt, **result, "event": "completed", "ended_at_utc": utc_now(),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        append_event(journal, completed)
        update_summary(run_dir, scenarios, summary)
        status = "API応答完了" if result["success"] else result["error_kind"]
        print(f"{scenario['scenario_id']} {scenario['category']}: {status}", flush=True)
        if not result["success"]:
            if result.get("diagnostic_code"):
                print(f"診断コード: {result['diagnostic_code']}、終了コード: {result['exit_code']}")
            print("最初の失敗で停止しました。未送信分を含むsummaryを保存済みです。診断後に判断してください。")
            return 1
    return 0


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="ローカル検証のみ。通信なし")
    mode.add_argument("--send", action="store_true", help="承認済みの範囲を送信する（課金あり）")
    parser.add_argument("--agent-name", help="送信先Hosted Agent名（送信時必須）")
    parser.add_argument("--agent-version", help="固定agent version（送信時必須）")
    parser.add_argument("--project-endpoint", help="azure.yamlと一致するproject endpoint（送信時必須）")
    parser.add_argument("--num-prompts", type=int, default=170, help="今回の件数。最大200")
    parser.add_argument("--scenario-count", type=int, default=170, help="固定manifest全体の件数。8～200")
    parser.add_argument("--offset", type=int, default=0, help="manifest内の開始位置（0始まり）")
    parser.add_argument("--stage", help="今回の一意な段階名。例: smoke、pilot、full")
    parser.add_argument("--seed", type=int, default=42, help="再現用seed")
    parser.add_argument("--concurrency", type=int, default=1, help="1のみ。azd会話cacheの競合を防ぐ直列実行")
    parser.add_argument("--timeout", type=int, default=300,
                        help="azdのHTTP応答timeout秒。起動・認証は別途最大240秒、自動再試行なし")
    parser.add_argument("--output-dir", type=Path, default=DEMO_DIR / "run", help="manifestと監査記録の保存先")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    args = argument_parser().parse_args(argv)
    try:
        if args.concurrency != 1:
            raise TrafficError("concurrencyは1のみです。azd共有会話cacheの競合防止のため直列実行します。")
        if (not 1 <= args.num_prompts <= 200 or args.offset < 0
                or args.offset + args.num_prompts > args.scenario_count):
            raise TrafficError("num-promptsは1～200件、offsetと件数はmanifestの範囲内で指定してください。")
        if not 1 <= args.timeout <= 3600:
            raise TrafficError("timeoutは1～3600秒で指定してください。")
        target = None
        if args.send:
            azd_environment()
            if not all((args.agent_name, args.agent_version, args.project_endpoint, args.stage)):
                raise TrafficError("送信にはagent-name、agent-version、project-endpoint、stageが必要です。")
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", args.agent_name)
                    or not re.fullmatch(r"[1-9][0-9]*", args.agent_version)):
                raise TrafficError("agent名または固定versionの形式が不正です。")
            endpoint = normalize_endpoint(args.project_endpoint)
            if endpoint != configured_endpoint():
                raise TrafficError("project endpointがazure.yamlと一致しません。送信しません。")
            target = {"agent_name": args.agent_name, "agent_version": args.agent_version,
                      "project_endpoint": endpoint}
            if not (args.output_dir / "traffic-scenarios.jsonl").exists():
                raise TrafficError("先にdry-runでmanifestを作成・確認してください。送信しません。")
        scenarios = generate_scenarios(args.seed, args.scenario_count)
        with run_lock(args.output_dir):
            summary = prepare_manifest(args.output_dir, scenarios, args.seed)
            update_summary(args.output_dir, scenarios, summary)
            if args.dry_run:
                print(f"通信なし: {len(scenarios)}件のmanifestを生成・照合しました。")
                print(json_text(summary["generation"]))
                print(f"選択範囲: offset={args.offset}, 件数={args.num_prompts}（送信なし）")
                return 0
            print(f"承認済み範囲を直列送信: offset={args.offset}, 件数={args.num_prompts}。課金が発生します。")
            return send_stage(args.output_dir, scenarios, summary, offset=args.offset,
                              count=args.num_prompts, stage=args.stage, target=target,
                              timeout=args.timeout)
    except TrafficError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断しました。記録を確認し、送信済み・結果不明のscenarioを再送しないでください。", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("エラー: ローカルファイルの読み書きに失敗しました。送信記録を診断してください。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

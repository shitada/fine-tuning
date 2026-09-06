import json
from pathlib import Path
from typing import Any

from agent_framework import FunctionTool, tool

from synthetic_store import (
    calculate_resolution as calculate_resolution_data,
    check_inventory as check_inventory_data,
    check_resolution_policy as check_resolution_policy_data,
    get_fulfillment_status as get_fulfillment_status_data,
    get_order_details as get_order_details_data,
    submit_resolution as submit_resolution_data,
)


CONTRACT_PATH = Path(__file__).resolve().parent / "contract" / "zava_tools.json"
TOOL_CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
TOOL_DEFINITIONS = {
    entry["function"]["name"]: entry["function"] for entry in TOOL_CONTRACT
}


def _definition(name: str) -> dict[str, Any]:
    return TOOL_DEFINITIONS[name]


@tool(
    name="get_order_details",
    description=_definition("get_order_details")["description"],
    schema=_definition("get_order_details")["parameters"],
    approval_mode="never_require",
)
def get_order_details(order_id: str) -> dict[str, Any]:
    return get_order_details_data(order_id)


@tool(
    name="get_fulfillment_status",
    description=_definition("get_fulfillment_status")["description"],
    schema=_definition("get_fulfillment_status")["parameters"],
    approval_mode="never_require",
)
def get_fulfillment_status(order_id: str) -> dict[str, Any]:
    return get_fulfillment_status_data(order_id)


@tool(
    name="check_resolution_policy",
    description=_definition("check_resolution_policy")["description"],
    schema=_definition("check_resolution_policy")["parameters"],
    approval_mode="never_require",
)
def check_resolution_policy(
    order_id: str,
    item_id: str,
    reason: str,
) -> dict[str, Any]:
    return check_resolution_policy_data(order_id, item_id, reason)


@tool(
    name="check_inventory",
    description=_definition("check_inventory")["description"],
    schema=_definition("check_inventory")["parameters"],
    approval_mode="never_require",
)
def check_inventory(sku: str) -> dict[str, Any]:
    return check_inventory_data(sku)


@tool(
    name="calculate_resolution",
    description=_definition("calculate_resolution")["description"],
    schema=_definition("calculate_resolution")["parameters"],
    approval_mode="never_require",
)
def calculate_resolution(
    order_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return calculate_resolution_data(order_id, items)


@tool(
    name="submit_resolution",
    description=_definition("submit_resolution")["description"],
    schema=_definition("submit_resolution")["parameters"],
    approval_mode="never_require",
)
def submit_resolution(
    order_id: str,
    calculation_id: str,
    resolution_summary: str,
) -> dict[str, Any]:
    return submit_resolution_data(order_id, calculation_id, resolution_summary)


ALL_TOOLS: list[FunctionTool] = [
    get_order_details,
    get_fulfillment_status,
    check_resolution_policy,
    check_inventory,
    calculate_resolution,
    submit_resolution,
]

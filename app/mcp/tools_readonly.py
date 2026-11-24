from __future__ import annotations

from mcp import types

TOOL_SPECS: dict[str, dict] = {
    "orders_recent_for_buyer": {
        "description": "List recent orders for the buyer of the current ticket. Scope is injected by the server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "buyer_external_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["buyer_external_id"],
            "additionalProperties": False,
        },
    },
    "order_get": {
        "description": "Get one order by order number within the authorized store scope.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_no": {"type": "string"}},
            "required": ["order_no"],
            "additionalProperties": False,
        },
    },
    "shipments_list": {
        "description": "List shipments for an authorized order.",
        "inputSchema": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
            "additionalProperties": False,
        },
    },
    "shipment_track": {
        "description": "Read tracking events for an authorized tracking number.",
        "inputSchema": {
            "type": "object",
            "properties": {"tracking_no": {"type": "string"}},
            "required": ["tracking_no"],
            "additionalProperties": False,
        },
    },
    "inventory_get": {
        "description": "Read inventory for a SKU within the authorized store scope.",
        "inputSchema": {
            "type": "object",
            "properties": {"sku": {"type": "string"}},
            "required": ["sku"],
            "additionalProperties": False,
        },
    },
}

TOOL_LIST = [
    types.Tool(
        name=name,
        description=spec["description"],
        inputSchema=spec["inputSchema"],
    )
    for name, spec in TOOL_SPECS.items()
]


def validate_arguments(spec: dict, arguments: dict) -> str | None:
    schema = spec["inputSchema"]
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    extra = set(arguments) - set(properties)
    if extra and schema.get("additionalProperties") is False:
        return f"unexpected arguments: {sorted(extra)}"
    for key in required:
        if key not in arguments:
            return f"missing required argument: {key}"
    for key, value in arguments.items():
        expected = properties.get(key, {}).get("type")
        if expected == "string" and not isinstance(value, str):
            return f"argument {key} must be a string"
        if expected == "integer" and not isinstance(value, int):
            return f"argument {key} must be an integer"
    return None
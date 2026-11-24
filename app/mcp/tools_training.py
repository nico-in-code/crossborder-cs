from __future__ import annotations

from mcp import types

TRAINING_TOOL_SPECS: dict[str, dict] = {
    "training_case_get": {
        "description": "Get one anonymized training case within the caller's tenant.",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    },
    "training_sop_get": {
        "description": "Get the fixed approved SOP version bound to a training case.",
        "inputSchema": {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        },
    },
}

TRAINING_TOOL_LIST = [
    types.Tool(
        name=name,
        description=spec["description"],
        inputSchema=spec["inputSchema"],
    )
    for name, spec in TRAINING_TOOL_SPECS.items()
]
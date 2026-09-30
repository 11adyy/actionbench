"""Frozen JSON contracts for model outputs; these are also part of request identity."""

from __future__ import annotations


VERSION = "actionbench-contract-v2"


def response_format(name: str, schema: dict) -> dict:
    return {"type": "json_schema", "name": name, "strict": True, "schema": schema}


def decision_format() -> dict:
    nullable_string = {"type": ["string", "null"]}
    return response_format("agent_decision_v2", {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["final", "code", "llm_code", "procedure"]},
            "answer": nullable_string,
            "code": nullable_string,
            "procedure_id": nullable_string,
            "input_json": nullable_string,
        },
        "required": ["type", "answer", "code", "procedure_id", "input_json"],
        "additionalProperties": False,
    })


def package_format(kind: str) -> dict:
    if kind == "skill":
        return response_format("skill_package_v2", {
            "type": "object", "properties": {"skill_md": {"type": "string"}},
            "required": ["skill_md"], "additionalProperties": False,
        })
    if kind not in {"skill_script", "action"}:
        raise ValueError(f"Unknown package kind: {kind}")
    procedure = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "description": {"type": "string"},
            "input_schema_json": {"type": "string"},
            "code": {"type": "string"},
        },
        "required": ["id", "description", "input_schema_json", "code"],
        "additionalProperties": False,
    }
    return response_format(f"{kind}_package_v2", {
        "type": "object",
        "properties": {"procedures": {"type": "array", "items": procedure, "minItems": 1, "maxItems": 2}},
        "required": ["procedures"], "additionalProperties": False,
    })

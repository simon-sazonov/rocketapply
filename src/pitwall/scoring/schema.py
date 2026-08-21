"""The JSON schema the LLM must return — §9.2 of PITWALL_PLAN.md.

Mirrored as pydantic models in :mod:`pitwall.models` (`LLMVerdict`,
`LLMBatchResponse`), which is what actually validates the reply. This module
holds the machine-readable schema so it can be embedded in the prompt and, on
providers that support it, passed as a response format.

The `same_name_different_org` enum value is the whole point: it is what lets
you separate McLaren Racing from McLaren Health Care, Williams Racing from
Williams Companies, Alpine F1 from Alpine the audio brand — programmatically,
with a reason string attached.
"""

from __future__ import annotations

from typing import Any

COMPANY_MATCH_VALUES = [
    "exact",
    "parent",
    "subsidiary",
    "same_name_different_org",
    "none",
    "unknown",
]
ROLE_MATCH_VALUES = ["exact", "adjacent", "unrelated", "unknown"]
SENIORITY_VALUES = [
    "intern",
    "graduate",
    "junior",
    "mid",
    "senior",
    "lead",
    "head",
    "executive",
    "unknown",
]

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "candidate_id",
        "is_person_profile",
        "is_current_employee",
        "company_match",
        "role_match",
        "seniority",
        "score",
        "confidence",
        "reasons",
    ],
    "additionalProperties": False,
    "properties": {
        "candidate_id": {"type": "integer"},
        "is_person_profile": {"type": "boolean"},
        "full_name": {"type": ["string", "null"]},
        "current_company": {"type": ["string", "null"]},
        "current_title": {"type": ["string", "null"]},
        "is_current_employee": {"type": ["boolean", "null"]},
        "company_match": {"type": "string", "enum": COMPANY_MATCH_VALUES},
        "role_match": {"type": "string", "enum": ROLE_MATCH_VALUES},
        "seniority": {"type": "string", "enum": SENIORITY_VALUES},
        "motorsport_experience": {"type": ["boolean", "null"]},
        "score": {"type": "number", "minimum": 0, "maximum": 100},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
}

BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["results"],
    "additionalProperties": False,
    "properties": {"results": {"type": "array", "items": RESULT_SCHEMA}},
}

#: Compact form pasted into the prompt so the model sees the contract.
SCHEMA_HINT = """{
  "results": [
    {
      "candidate_id": <int, echo the value given>,
      "is_person_profile": <bool>,
      "full_name": <string|null>,
      "current_company": <string|null>,
      "current_title": <string|null>,
      "is_current_employee": <bool|null>,
      "company_match": "exact|parent|subsidiary|same_name_different_org|none|unknown",
      "role_match": "exact|adjacent|unrelated|unknown",
      "seniority": "intern|graduate|junior|mid|senior|lead|head|executive|unknown",
      "motorsport_experience": <bool|null>,
      "score": <0-100>,
      "confidence": <0.0-1.0>,
      "reasons": [<1-3 short strings quoting the evidence>],
      "red_flags": [<strings, may be empty>]
    }
  ]
}"""


def response_format(strict: bool = False) -> dict[str, Any]:
    """`response_format` for the Chat Completions call.

    Plain `json_object` by default — it is what every OpenAI-compatible free
    tier supports. `strict=True` upgrades to a full json_schema for providers
    that accept it.
    """
    if not strict:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {"name": "pitwall_batch", "strict": True, "schema": BATCH_SCHEMA},
    }

"""Trusted D3 task constraints; a task description cannot relax field policy."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re

PROFILE_VERSION = "d3-task-profile-1"
TASKS = {"collaboration_statistics", "record_summary", "incident_diagnostics"}


def strict_json(payload):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(payload, object_pairs_hook=unique)
    except (RecursionError, TypeError):
        raise ValueError("invalid JSON structure") from None


def read_config(path):
    with Path(path).open("rb") as stream:
        payload = stream.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("configuration size limit")
    return payload


@dataclass(frozen=True)
class FieldPolicy:
    name: str = field(repr=False)
    output_name: str
    mode: str = "keep"
    required: bool = True
    approved_values: tuple[str, ...] = field(default=(), repr=False)
    generalizations: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name or len(self.name) > 128:
            raise ValueError("invalid task field")
        if not isinstance(self.output_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", self.output_name) or self.output_name == "assertion_state":
            raise ValueError("invalid public field name")
        if self.mode not in {"keep", "alias", "generalize", "drop"} or type(self.required) is not bool:
            raise ValueError("invalid field constraint")
        if self.required and self.mode == "drop":
            raise ValueError("required field cannot be dropped")
        if not isinstance(self.approved_values, tuple) or len(self.approved_values) > 1000 or any(not isinstance(v, str) or len(v) > 65536 for v in self.approved_values):
            raise ValueError("invalid value allowlist")
        if not isinstance(self.generalizations, tuple) or len(self.generalizations) > 1000:
            raise ValueError("invalid generalizations")
        if any(not isinstance(p, tuple) or len(p) != 2 or any(not isinstance(v, str) or not v or len(v) > 65536 for v in p) for p in self.generalizations):
            raise ValueError("invalid generalization pair")
        if len({a for a, _ in self.generalizations}) != len(self.generalizations):
            raise ValueError("ambiguous generalization")
        if (self.mode == "generalize") != bool(self.generalizations):
            raise ValueError("generalization mode requires an explicit map")


@dataclass(frozen=True)
class TaskProfile:
    task: str
    recipient: str = field(repr=False)
    fields: tuple[FieldPolicy, ...] = field(repr=False)
    preserve_rows: bool = True
    preserve_relations: bool = True
    forbidden_terms: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if self.task not in TASKS:
            raise ValueError("unsupported task profile")
        if not isinstance(self.recipient, str) or not self.recipient or len(self.recipient) > 256:
            raise ValueError("recipient required")
        if not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 128 or any(not isinstance(f, FieldPolicy) for f in self.fields):
            raise ValueError("invalid task fields")
        if len({f.name for f in self.fields}) != len(self.fields) or len({f.output_name for f in self.fields}) != len(self.fields):
            raise ValueError("ambiguous task fields")
        if type(self.preserve_rows) is not bool or type(self.preserve_relations) is not bool:
            raise ValueError("invalid preservation constraints")
        if not isinstance(self.forbidden_terms, tuple) or len(self.forbidden_terms) > 1000 or any(not isinstance(s, str) or not s or len(s) > 1024 for s in self.forbidden_terms):
            raise ValueError("invalid forbidden terms")
        if not any(f.required and f.mode in {"keep", "generalize"} for f in self.fields):
            raise ValueError("task requires at least one retained factual field")


def load_task_profile(path):
    data = strict_json(read_config(path).decode("utf-8-sig"))
    try:
        if set(data) - {"schema_version", "task", "recipient", "fields", "preserve_rows", "preserve_relations", "forbidden_terms"} or data["schema_version"] != PROFILE_VERSION:
            raise ValueError("invalid task profile schema")
        if not isinstance(data.get("forbidden_terms", []), list):
            raise ValueError("invalid forbidden terms")
        fields = []
        for source in data["fields"]:
            item = dict(source)
            if "approved_values" in item:
                if not isinstance(item["approved_values"], list):
                    raise ValueError("invalid approved values")
                item["approved_values"] = tuple(item["approved_values"])
            if "generalizations" in item:
                if not isinstance(item["generalizations"], dict):
                    raise ValueError("invalid generalization map")
                item["generalizations"] = tuple(item["generalizations"].items())
            fields.append(FieldPolicy(**item))
        return TaskProfile(data["task"], data["recipient"], tuple(fields), data.get("preserve_rows", True),
                           data.get("preserve_relations", True), tuple(data.get("forbidden_terms", ())))
    except (TypeError, KeyError, AttributeError):
        raise ValueError("invalid task profile") from None

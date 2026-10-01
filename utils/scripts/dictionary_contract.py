"""Versioned file contracts shared by the dictionary app and future skills.

Run ``python -m utils.scripts.dictionary_contract generate --root .`` to refresh
the checked-in schemas and the application's default color configuration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, ValidationError

from utils.scripts.structured_io import write_text_atomic

VERSION = "1.0"
COLOR = {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}
STAMP = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"}
WORD_ID = r"^w_[0-9a-f]{20}$"
SENSE_ID = r"^s_[0-9a-f]{20}$"
ITEM_ID = r"^i_[0-9a-f]{20}$"


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required or list(properties)}


def base_state(extra: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            **obj({"schemaVersion": {"const": VERSION},
                   "revision": {"type": "integer", "minimum": 1},
                   "updatedAt": STAMP, **extra})}


SCHEMAS: dict[str, dict[str, Any]] = {
    "dictionary-learning-v1.schema.json": base_state({
        "words": {"type": "object", "patternProperties": {WORD_ID: {
            "enum": ["unfamiliar", "seen", "familiar"]}}, "additionalProperties": False}}),
    "dictionary-favorites-v1.schema.json": base_state({
        "words": {"type": "object", "patternProperties": {WORD_ID: obj({
            "favoritedAt": STAMP, "lastOpenedAt": STAMP})}, "additionalProperties": False}}),
    "dictionary-ui-v1.schema.json": base_state({
        "activeProjectId": {"type": "string", "minLength": 1},
        "activeStageId": {"type": "string", "minLength": 1},
        "tabs": {"type": "array", "items": obj({
            "tabId": {"type": "string", "minLength": 1},
            "kind": {"enum": ["graph", "word", "favorites", "settings", "candidate"]},
            "wordId": {"type": ["string", "null"]},
            "lemma": {"type": ["string", "null"]},
            "pinned": {"type": "boolean"}})},
        "activeTabId": {"type": "string", "minLength": 1},
        "study": obj({"openPlanIds": {"type": "array", "uniqueItems": True,
                                       "items": {"type": "string", "pattern": "^plan_[0-9a-f]{32}$"}},
                      "activeTabId": {"type": "string", "minLength": 1}}),
        "split": obj({"enabled": {"type": "boolean"},
                      "leftWidthPercent": {"type": "number", "minimum": 25, "maximum": 75}}),
        "graph": obj({
            "zoomPercent": {"type": "integer", "minimum": 25, "maximum": 400},
            "positions": {"type": "object", "patternProperties": {r"^(?:w_|candidate:)[0-9a-f]{20}$": obj({
                "x": {"type": "number"}, "y": {"type": "number"}})}, "additionalProperties": False},
            "visibleRelations": obj({key: {"type": "boolean"} for key in
                                     ("family", "synonym", "near_synonym", "antonym", "spelling_similar")})})}),
    "dictionary-stage-v1.schema.json": base_state({
        "stageId": {"type": "string", "minLength": 1},
        "label": {"type": "string", "minLength": 1},
        "words": {"type": "object", "patternProperties": {WORD_ID: obj({
            "sourceEntryRevision": {"type": "integer", "minimum": 1},
            "coreSenseIds": {"type": "array", "uniqueItems": True, "items": {"type": "string", "pattern": SENSE_ID}},
            "moreSenseIds": {"type": "array", "uniqueItems": True, "items": {"type": "string", "pattern": SENSE_ID}},
            "contentIdsBySense": {"type": "object", "patternProperties": {SENSE_ID: {
                "type": "array", "uniqueItems": True, "items": {"type": "string", "pattern": ITEM_ID}}},
                "additionalProperties": False},
            "relationshipIds": {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}}
        })}, "additionalProperties": False}}),
    "dictionary-project-v1.schema.json": base_state({
        "projectId": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
        "name": {"type": "string", "minLength": 1, "maxLength": 100},
        "words": {"type": "array", "uniqueItems": True, "items": obj({
            "wordId": {"type": "string", "pattern": WORD_ID},
            "lemma": {"type": "string", "minLength": 1, "maxLength": 100}})}}),
    "dictionary-config-v1.schema.json": {"$schema": "https://json-schema.org/draft/2020-12/schema", **obj({
        "schemaVersion": {"const": VERSION},
        "ui": obj({k: COLOR for k in ("background", "surface", "surfaceRaised", "text", "mutedText",
                                        "border", "hover", "selected", "disabled", "error", "success")}),
        "learning": obj({k: COLOR for k in ("unfamiliar", "seen", "familiar", "outsideStage")}),
        "graph": obj({"initializeOnEnter": {"type": "boolean"}, "family": COLOR, "relations": obj({k: COLOR for k in
                          ("synonym", "near_synonym", "antonym", "spelling_similar")}),
                       "physics": obj({"springStrength": {"type": "number", "minimum": 0, "maximum": 1},
                                      "repulsionStrength": {"type": "number", "minimum": 0, "maximum": 5000},
                                        "familyNonMemberRepulsionStrength": {"type": "number", "minimum": 0, "maximum": 5000},
                                      "damping": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                                      "restLength": {"type": "number", "minimum": 30, "maximum": 500},
                                      "familyNodeGap": {"type": "number", "minimum": 0, "maximum": 64}})}),
        "controls": obj({k: COLOR for k in ("activeText", "inactiveText", "focus", "sliderTrack")}),
        "favorites": obj({"star": COLOR}),
        "study": obj({"wordsPerPage": {"type": "integer", "minimum": 1, "maximum": 100}})})},
}

DEFAULT_CONFIG = {
    "schemaVersion": VERSION,
    "ui": {"background": "#F4F1E9", "surface": "#FFFEFA", "surfaceRaised": "#EAE6DA",
           "text": "#20302C", "mutedText": "#61706B", "border": "#D4D8CE",
           "hover": "#E5EEE6", "selected": "#D7EAD9", "disabled": "#A8B0A8",
           "error": "#B43E3E", "success": "#22744D"},
    "learning": {"unfamiliar": "#F5D7D4", "seen": "#D7E9F5", "familiar": "#D8EEDB",
                 "outsideStage": "#D9DDDB"},
    "graph": {"initializeOnEnter": True, "family": "#8B5CF6", "relations": {"synonym": "#16A34A",
                "near_synonym": "#0EA5E9", "antonym": "#EF4444", "spelling_similar": "#F59E0B"},
               "physics": {"springStrength": 0.08, "repulsionStrength": 900,
                           "familyNonMemberRepulsionStrength": 1200,
                          "damping": 0.82, "restLength": 150, "familyNodeGap": 8}},
    "controls": {"activeText": "#173A2B", "inactiveText": "#68746E", "focus": "#1B6C53",
                 "sliderTrack": "#A9BEB1"},
    "favorites": {"star": "#F2B524"},
    "study": {"wordsPerPage": 10},
}


def validate(kind: str, value: Any) -> None:
    schema = SCHEMAS[kind]
    if kind == "dictionary-project-v1.schema.json" and isinstance(value, dict) and isinstance(value.get("words"), list):
        # jsonschema compares object items pairwise for uniqueItems. Large exam
        # word lists make that quadratic, so check the same constraint first.
        words = value["words"]
        if len({json.dumps(item, ensure_ascii=False, sort_keys=True) for item in words}) != len(words):
            raise ValidationError("项目词表包含重复项")
        schema = {**schema, "properties": {**schema["properties"], "words": {
            **schema["properties"]["words"], "uniqueItems": False}}}
    Draft202012Validator(schema).validate(value)


def default_state(kind: str, now: str) -> dict[str, Any]:
    common = {"schemaVersion": VERSION, "revision": 1, "updatedAt": now}
    if kind in ("learning", "favorites"):
        return {**common, "words": {}}
    if kind == "ui":
        return {**common, "activeProjectId": "default", "activeStageId": "all", "tabs": [
            {"tabId": "graph", "kind": "graph", "wordId": None, "lemma": None, "pinned": True}],
            "activeTabId": "graph", "study": {"openPlanIds": [], "activeTabId": "home"},
            "split": {"enabled": True, "leftWidthPercent": 52},
            "graph": {"zoomPercent": 100, "positions": {}, "visibleRelations": {
                key: True for key in ("family", "synonym", "near_synonym", "antonym", "spelling_similar")}}}
    raise ValueError(kind)


def default_stage(stage_id: str, label: str, now: str) -> dict[str, Any]:
    if not stage_id or not all(ch.isalnum() or ch in "_-" for ch in stage_id):
        raise ValueError("无效 stageId")
    value = {"schemaVersion": VERSION, "revision": 1, "updatedAt": now,
             "stageId": stage_id, "label": label, "words": {}}
    validate("dictionary-stage-v1.schema.json", value)
    return value


def default_project(project_id: str, name: str, words: list[dict[str, str]], now: str) -> dict[str, Any]:
    value = {"schemaVersion": VERSION, "revision": 1, "updatedAt": now,
             "projectId": project_id, "name": name, "words": words}
    validate("dictionary-project-v1.schema.json", value)
    return value


def generate(root: Path) -> None:
    reference_dir = root / "utils" / "references"
    for name, schema in SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        write_text_atomic(reference_dir / name, json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    target = root / "applications" / "词汇星图" / "config.yaml"
    if not target.exists():
        validate("dictionary-config-v1.schema.json", DEFAULT_CONFIG)
        write_text_atomic(target, yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["generate", "template"])
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--kind", choices=["learning", "favorites", "ui", "stage", "project"])
    parser.add_argument("--stage-id")
    parser.add_argument("--project-id")
    parser.add_argument("--label")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "generate":
        generate(args.root.resolve())
    else:
        if not args.kind or not args.output:
            parser.error("template 需要 --kind 和 --output")
        from utils.scripts.timestamp import iso_timestamp
        value = (default_stage(args.stage_id or "", args.label or "", iso_timestamp()) if args.kind == "stage"
                 else default_project(args.project_id or "", args.label or "", [], iso_timestamp()) if args.kind == "project"
                 else default_state(args.kind, iso_timestamp()))
        validate(f"dictionary-{args.kind}-v1.schema.json", value)
        write_text_atomic(args.output, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

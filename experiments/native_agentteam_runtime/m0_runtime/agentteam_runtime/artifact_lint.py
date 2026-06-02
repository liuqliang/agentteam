import argparse
import json
from pathlib import Path


EVENT_REQUIRED_FIELDS = {
    "event_id",
    "sequence",
    "time",
    "event_type",
    "actor",
    "idempotency_key",
    "correlation_id",
    "payload",
}


def lint_artifacts(root_path):
    root_path = Path(root_path)
    errors = []
    json_files = sorted(root_path.rglob("*.json"))
    jsonl_files = sorted(root_path.rglob("*.jsonl"))

    for path in json_files:
        _read_json(path, root_path, errors)

    event_type_enum = _load_event_type_enum(root_path)
    for path in jsonl_files:
        _lint_jsonl(path, root_path, errors, event_type_enum)

    return {
        "status": "failed" if errors else "passed",
        "root_path": str(root_path),
        "checked_json_files": len(json_files),
        "checked_jsonl_files": len(jsonl_files),
        "errors": errors,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lint AgentTeam native runtime artifacts.")
    parser.add_argument("--root", required=True, help="Root directory containing artifacts to lint.")
    args = parser.parse_args(argv)
    summary = lint_artifacts(args.root)
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1


def _read_json(path, root_path, errors):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(
            {
                "kind": "invalid_json",
                "path": _relative_path(path, root_path),
                "message": str(exc),
            }
        )
        return None


def _lint_jsonl(path, root_path, errors, event_type_enum):
    expected_sequence = 1
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(
                {
                    "kind": "invalid_jsonl",
                    "path": _relative_path(path, root_path),
                    "line": line_number,
                    "message": str(exc),
                }
            )
            continue
        if _is_event_record(path, record):
            expected_sequence = _lint_event_record(
                path,
                root_path,
                errors,
                event_type_enum,
                line_number,
                record,
                expected_sequence,
            )
            continue
        _lint_event_type(path, root_path, errors, event_type_enum, line_number, record)


def _lint_event_record(path, root_path, errors, event_type_enum, line_number, record, expected_sequence):
    missing_fields = sorted(EVENT_REQUIRED_FIELDS - set(record.keys()))
    if missing_fields:
        errors.append(
            {
                "kind": "missing_event_fields",
                "path": _relative_path(path, root_path),
                "line": line_number,
                "missing_fields": missing_fields,
            }
        )
    _lint_event_sequence(path, root_path, errors, line_number, record, expected_sequence)
    _lint_event_type(path, root_path, errors, event_type_enum, line_number, record)
    sequence = record.get("sequence")
    return sequence + 1 if isinstance(sequence, int) else expected_sequence


def _is_event_record(path, record):
    return (
        path.name == "events.jsonl"
        or isinstance(record, dict)
        and ("event_type" in record or "event_id" in record or "sequence" in record)
    )


def _lint_event_sequence(path, root_path, errors, line_number, record, expected_sequence):
    sequence = record.get("sequence")
    if not isinstance(sequence, int):
        return
    if sequence != expected_sequence:
        errors.append(
            {
                "kind": "non_monotonic_event_sequence",
                "path": _relative_path(path, root_path),
                "line": line_number,
                "expected_sequence": expected_sequence,
                "actual_sequence": sequence,
            }
        )


def _lint_event_type(path, root_path, errors, event_type_enum, line_number, record):
    event_type = record.get("event_type") if isinstance(record, dict) else None
    if event_type is None or event_type_enum is None:
        return
    if event_type not in event_type_enum:
        errors.append(
            {
                "kind": "invalid_event_type",
                "path": _relative_path(path, root_path),
                "line": line_number,
                "event_type": event_type,
            }
        )


def _load_event_type_enum(root_path):
    schema_path = root_path / "schemas" / "event.schema.json"
    if not schema_path.exists():
        return None
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    enum_values = (
        schema.get("properties", {})
        .get("event_type", {})
        .get("enum")
    )
    if not isinstance(enum_values, list):
        return None
    return set(enum_values)


def _relative_path(path, root_path):
    try:
        return path.relative_to(root_path).as_posix()
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())

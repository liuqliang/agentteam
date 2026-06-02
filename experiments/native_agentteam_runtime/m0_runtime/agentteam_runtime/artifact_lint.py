import argparse
import json
from pathlib import Path


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
        _lint_event_type(path, root_path, errors, event_type_enum, line_number, record)


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

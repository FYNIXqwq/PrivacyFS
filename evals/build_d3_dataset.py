"""Freeze small structured-task utility cases, never overwrite a version."""
from pathlib import Path
import csv
import hashlib
import io
import json


def csv_text(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def case(family, variant):
    task = ["collaboration_statistics", "record_summary", "incident_diagnostics"][(family + variant) % 3]
    fact_name = ["status", "summary_fact", "error_code"][["collaboration_statistics", "record_summary", "incident_diagnostics"].index(task)]
    client = f"CLIENTKEY{family}_{variant}"
    name = f"合成主体{family}号"
    rows = 1 + variant % 3
    facts = [f"FACT{family}_A", f"FACT{family}_B"]
    if variant == 4:
        facts[0] += ',"quoted"\nline'
    state = {1: "negative", 2: "quoted"}.get(variant, "positive")
    state_field = "state"
    ckey = {"name": "cid", "role": "key", "namespace": f"clients{family}", "entity_type": "client"}
    mkey = {"name": "mid", "role": "key", "namespace": f"meetings{family}", "entity_type": "event"}
    templates = [
        {"primary": "cid", "fields": [ckey, {"name": "name", "role": "identity"}]},
        {"primary": "cid", "fields": [ckey, mkey]},
        {"primary": "mid", "fields": [mkey, {"name": fact_name, "role": "sensitive", "values": facts}], "state_field": state_field}]
    names = ["source.csv", "bridge.csv", "notes.csv" if variant == 4 else "notes.md" if family % 2 else "notes.txt"]
    note_rows = [[f"M{family}_{variant}_{i}", facts[i % 2], state] for i in range(rows)]
    texts = [csv_text(["cid", "name", "unconfigured"], [[client, name, f"PRIVATE_UNUSED_{family}_{variant}"]]),
             csv_text(["cid", "mid"], [[client, row[0]] for row in note_rows])]
    if names[2].endswith("csv"):
        texts.append(csv_text(["mid", fact_name, state_field], note_rows))
    else:
        texts.append("".join(f"mid={m};{fact_name}={value};state={s}\n" for m, value, s in note_rows))
    policy = {"name": fact_name, "output_name": "fact", "approved_values": facts}
    expected = [row[1] for row in note_rows]
    if variant == 5:
        policy = {"name": fact_name, "output_name": "fact", "mode": "generalize", "generalizations": {f: f"BUCKET{family}" for f in facts}}
        expected = [f"BUCKET{family}"] * rows
    profile = {"schema_version": "d3-task-profile-1", "task": task, "recipient": f"synthetic-recipient-{family}",
               "fields": [{"name": "cid", "output_name": "client_id", "mode": "alias"},
                          {"name": "mid", "output_name": "meeting_id", "mode": "alias"}, policy]}
    return {"id": f"d3-f{family:02d}-v{variant}", "group": f"task-family-{family}",
            "split": "validation" if family in {2, 5, 8} else "development",
            "documents": [{"path": p, "text": text, "template": t} for p, text, t in zip(names, texts, templates)],
            "profile": profile, "expected_facts": expected, "expected_state": state,
            "forbidden": [name, client, f"PRIVATE_UNUSED_{family}_{variant}", *[r[0] for r in note_rows]]}


def main():
    path = Path(__file__).parent / "data" / "d3-v1.jsonl"
    if path.exists() or path.with_suffix(".manifest.json").exists():
        raise SystemExit("version exists; use a new version")
    cases = [case(family, variant) for family in range(10) for variant in range(6)]
    payload = "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n" for c in cases).encode("utf-8")
    path.write_bytes(payload)
    path.with_suffix(".manifest.json").write_text(json.dumps({"version": "d3-v1", "sha256": hashlib.sha256(payload).hexdigest(),
        "cases": 60, "families": 10, "split_policy": "whole_task_family", "synthetic": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

"""Generate the versioned synthetic D2 graph corpus; never overwrite a version."""
from pathlib import Path
import hashlib
import json


def build_case(family, variant):
    # A family fixes topology, namespace/type, field vocabulary and wrappers.
    # All its positive/negative perturbations remain in a single split.
    depth = 1 + family % 4
    split = "validation" if family in {3, 6, 9, 12, 15} else "development"
    fields = {k: f"f{family}_{k}" for k in ("key", "name", "ref", "status", "state", "city", "job")}
    docs, occurrences = [], []
    ptype = ("client", "person", "organization", "project")[family // 4]
    namespace = f"family{family}_subject"
    client = f"C{family:02d}001"

    def spec(name, role, ns="", kind="client", values=None):
        result = {"name": fields[name], "role": role, "namespace": ns, "entity_type": kind}
        if values is not None:
            result["values"] = values
        return result

    def document(name, row, specs, groups, *, state=None, quoted=False, literal=False):
        names = [s["name"] for s in specs]
        if state is not None:
            names.append(fields["state"])
            row = [*row, state]
        path = name + (".md" if literal else ".csv")
        if literal:
            text = (">" if quoted else "") + ";".join(f"{k}={v}" for k, v in zip(names, row)) + "\n"
        else:
            text = ",".join(names) + "\n" + ",".join(row) + "\n"
        config = {"primary": names[0], "fields": specs}
        if state is not None:
            config["state_field"] = fields["state"]
        docs.append({"path": path, "text": text, "template": config})
        # Gold source spans computed directly from serialized synthetic records.
        cursor = (1 if quoted else 0) if literal else text.index("\n") + 1
        for index, (key, value) in enumerate(zip(names, row)):
            start = cursor + len(key) + 1 if literal else cursor
            if index < len(specs) and specs[index]["role"] == "key":
                occurrences.append({"path": path, "start": start, "end": start + len(value), "entity": groups[index]})
            cursor += len(key) + 1 + len(value) + 1 if literal else len(value) + 1

    identity = variant not in {12, 14}
    document("identity", [client, f"SyntheticName{family}"],
             [spec("key", "key", namespace, ptype), spec("name", "identity" if variant != 12 else "label")],
             {0: "subject"}, state="unknown" if variant == 14 else None, literal=family % 2 == 1)
    for step in range(depth):
        first = client if step == 0 else f"N{family}_{step}"
        last = f"N{family}_{step + 1}"
        ns = namespace if step == 0 else f"family{family}_node{step}"
        ns_next = f"family{family}_node{step + 1}"
        kind = ptype if step == 0 else "event"
        group = "subject" if step == 0 else f"node{step}"
        if step == 0 and variant in {1, 10}:
            first = f"unrelated{family}" if variant == 1 else client[1:]
            group = "wrong_subject"
        if step == 0 and variant == 6:
            kind = "event" if ptype != "event" else "client"
            group = "wrong_type"
        specs = [spec("key", "key", ns, kind), spec("ref", "key", ns_next, "event")]
        document(f"bridge{step}", [first, last], specs, {0: group, 1: f"node{step+1}"}, literal=(family+step) % 3 == 0)
        if variant == 7:
            document(f"copy{step}", [first, last], specs, {0: group, 1: f"node{step+1}"})
    end_namespace = f"family{family}_node{depth}" + ("_other" if variant == 2 else "")
    document("sensitive", [f"N{family}_{depth}", "unlisted" if variant == 5 else "secret"],
             [spec("key", "key", end_namespace, "event"), spec("status", "sensitive", values=["secret", "ended"])],
             {0: "wrong_namespace" if variant == 2 else f"node{depth}"},
             state="negative" if variant == 3 else None, quoted=variant == 4, literal=True)
    # Quasi identifiers are deliberately split between two files.
    for field_name, value in (("city", f"City{family}"), ("job", f"Job{family}")):
        document(field_name, [client, value], [spec("key", "key", namespace, ptype), spec(field_name, "quasi")], {0: "subject"})
    if variant == 11:
        document("conflicting_qid", [client, f"OtherCity{family}"],
                 [spec("key", "key", namespace, ptype), spec("city", "quasi")], {0: "subject"})
    if variant in {8, 9, 13, 15}:
        # A second independent subject with the SAME name must remain separate.
        document("same_name", [f"Other{family}", f"SyntheticName{family}"],
                 [spec("key", "key", namespace, ptype), spec("name", "identity")], {0: "other_subject"})
    extra_identity = int(variant in {8, 9, 13, 15})
    joined = identity and variant not in {1, 2, 3, 4, 5, 6, 10}
    return {"id": f"d2-f{family:02d}-v{variant:02d}", "group": f"topology-{family:02d}", "split": split,
            "documents": docs, "occurrences": occurrences,
            "quasi_fields": [fields["city"], fields["job"]],
            "expected_risks": {"identity_mapping": int(identity) + extra_identity,
                               "numbered_join": int(joined), "quasi_combination": int(variant != 11)},
            "expected_incremental_joins": int(joined)}


def main():
    destination = Path(__file__).parent / "data" / "d2-v1.jsonl"
    manifest = destination.with_suffix(".manifest.json")
    if destination.exists() or manifest.exists():
        raise SystemExit("version already exists; choose a new version")
    cases = [build_case(family, variant) for family in range(16) for variant in range(16)]
    payload = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases).encode("utf-8")
    destination.write_bytes(payload)
    manifest.write_text(json.dumps({"version": "d2-v1", "cases": len(cases), "families": 16,
        "sha256": hashlib.sha256(payload).hexdigest(), "split_policy": "whole_topology_namespace_wrapper_family",
        "validation_families": [3, 6, 9, 12, 15], "synthetic": True}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

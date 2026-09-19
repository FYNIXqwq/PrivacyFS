"""Frozen benchmark runner; reports contain safe IDs/counts, never raw guesses."""
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import platform
import re
import tempfile
import time

from privacyfs.normalization import normalize_with_map
from privacyfs.task_profiles import strict_json

from .adapters import (DETECTORS, TREATMENT_VERSIONS, DetectionInput, make_release_input,
                       transform, task_score)
from .attacks import ATTACKS, AttackView, Knowledge, Claims
from .metrics import score, combine, attack_summary, paired_attacks

PROTOCOL_VERSION = "public-tables-and-priors-v1"


def load_dataset(path):
    path = Path(path)
    with path.open("rb") as stream:
        payload = stream.read(4_194_305)
    if len(payload) > 4_194_304:
        raise ValueError("dataset limit")
    manifest = strict_json(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    if manifest.get("synthetic") is not True or hashlib.sha256(payload).hexdigest() != manifest.get("sha256"):
        raise ValueError("dataset provenance or hash mismatch")
    cases = [strict_json(line) for line in payload.decode("utf-8").splitlines() if line.strip()]
    families, ids, documents = {}, set(), {}
    if not cases or len(cases) > 1000:
        raise ValueError("empty or oversized dataset")
    for case in cases:
        if not re.fullmatch(r"[a-z0-9-]{1,64}", case["id"]) or case["id"] in ids:
            raise ValueError("invalid case id")
        ids.add(case["id"])
        split = case["split"]
        if split not in {"development", "validation"} or families.setdefault(case["family"], split) != split:
            raise ValueError("family partition mismatch")
        if case["suite"] == "detection":
            texts = case["documents"]
            if not isinstance(texts, list) or not 1 <= len(texts) <= 20:
                raise ValueError("invalid detection documents")
            for field in ("names", "organizations"):
                if not isinstance(case["policy"][field], list) or any(not isinstance(v, str) for v in case["policy"][field]):
                    raise ValueError("invalid detector policy")
            seen = set()
            for m in case["gold"]["mentions"]:
                if any(type(m[k]) is not int for k in ("document", "start", "end")):
                    raise ValueError("invalid gold location")
                if not 0 <= m["document"] < len(texts):
                    raise ValueError("invalid gold document")
                text = texts[m["document"]]
                if m["category"] not in {"PERSON", "ORG"} or not 0 <= m["start"] < m["end"] <= len(text):
                    raise ValueError("invalid gold span")
                if text[m["start"]:m["end"]] != m["surface"]:
                    raise ValueError("gold locator mismatch")
                key = (m["document"], m["category"], m["start"], m["end"])
                if key in seen:
                    raise ValueError("duplicate gold mention")
                seen.add(key)
        elif case["suite"] == "release":
            expected = {"clients": ["cid", "name"], "bridges": ["cid", "mid"],
                        "facts": ["mid", "topic", "fact", "assertion_state"]}
            if len(case["tables"]) != 3 or {t["kind"] for t in case["tables"]} != set(expected):
                raise ValueError("invalid release tables")
            texts = []
            for t in case["tables"]:
                if t["columns"] != expected[t["kind"]] or not 1 <= len(t["rows"]) <= 100:
                    raise ValueError("invalid table schema")
                if any(len(r) != len(t["columns"]) or any(not isinstance(v, str) or len(v) > 4096 for v in r) for r in t["rows"]):
                    raise ValueError("invalid table rows")
                texts.append(json.dumps(t, sort_keys=True, ensure_ascii=False))
            for key in ("background", "history"):
                if len(case[key]) > 100 or any(set(k) != {"identity", "key", "topic"}
                    or any(not isinstance(v, str) or len(v) > 4096 for v in k.values()) for k in case[key]):
                    raise ValueError("invalid prior knowledge")
            subjects = case["gold"]["subjects"]
            if not subjects or len({s["subject"] for s in subjects}) != len(subjects):
                raise ValueError("invalid subject gold")
            for s in subjects:
                if set(s) != {"subject", "identity", "facts"} or not s["subject"] or not s["identity"]:
                    raise ValueError("invalid subject gold")
                if not isinstance(s["facts"], list) or any(not isinstance(v, str) for v in s["facts"]):
                    raise ValueError("invalid fact gold")
        else:
            raise ValueError("unknown suite")
        for text in texts:
            if not isinstance(text, str) or len(text) > 100_000:
                raise ValueError("document limit")
            normalized = normalize_with_map(text).text
            if documents.setdefault(normalized, split) != split:
                raise ValueError("normalized document crosses partitions")
    if len(cases) != manifest["cases"] or len(families) != manifest["families"]:
        raise ValueError("manifest counts mismatch")
    return cases, manifest


def request_for(case_id, treatment, view):
    request = {"schema_version": PROTOCOL_VERSION, "request_id": f"{case_id}/{treatment}",
               "view": asdict(view)}
    request["request_sha256"] = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                                         separators=(",", ":")).encode()).hexdigest()
    return request


def load_responses(path):
    with Path(path).open("rb") as stream:
        payload = stream.read(8_388_609)
    if len(payload) > 8_388_608:
        raise ValueError("response limit")
    data = strict_json(payload)
    if set(data) != {"schema_version", "attacker", "responses"} or data["schema_version"] != PROTOCOL_VERSION:
        raise ValueError("response schema mismatch")
    metadata = data["attacker"]
    if set(metadata) != {"implementation", "version", "provider", "model", "mode"}:
        raise ValueError("attacker metadata required")
    if any(not isinstance(v, str) or not v or len(v) > 128 for v in metadata.values()):
        raise ValueError("invalid attacker metadata")
    if metadata["mode"] not in {"mock", "live"}:
        raise ValueError("invalid execution mode")
    rows = data["responses"]
    if len(rows) > 4000 or len({r["request_id"] for r in rows}) != len(rows):
        raise ValueError("duplicate or excessive responses")
    return metadata, {r["request_id"]: r for r in rows}


def response_claims(response, request):
    if set(response) != {"request_id", "request_sha256", "status", "identities", "attributes"}:
        raise ValueError("invalid attack response")
    if response["request_sha256"] != request["request_sha256"]:
        raise ValueError("stale attack response")
    if response["status"] not in {"complete", "failed", "timeout", "invalid"}:
        raise ValueError("invalid attack status")
    for key in ("identities", "attributes"):
        rows = response[key]
        if not isinstance(rows, list) or len(rows) > 1000 or any(not isinstance(r, list) or len(r) != 2
                or any(not isinstance(v, str) or not v or len(v) > 4096 for v in r) for r in rows):
            raise ValueError("invalid claims")
        if response["status"] != "complete" and rows:
            raise ValueError("failed response contains claims")
    return Claims(frozenset(map(tuple, response["identities"])), frozenset(map(tuple, response["attributes"])))


def run(path, split="validation", *, detectors=None, attackers=None, requests=None, responses_path=None,
        include_cross_file=False):
    if split not in {"development", "validation", "all"}:
        raise ValueError("unknown split")
    cases, manifest = load_dataset(path)
    selected = [c for c in cases if split == "all" or c["split"] == split]
    if not selected:
        raise ValueError("empty selection")
    detectors = DETECTORS if detectors is None else detectors
    attackers = ATTACKS if attackers is None else attackers
    metadata, responses = load_responses(responses_path) if responses_path else (None, {})
    used_responses = set()
    errors, case_results, det_records = [], [], {name: [] for name in detectors}
    attack_records = {t: {name: [] for name in attackers} for t in TREATMENT_VERSIONS}
    if responses_path:
        for rows in attack_records.values():
            rows["pi_agent"] = []
    utility_records = {t: [] for t in TREATMENT_VERSIONS}
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="privacyfs-effectiveness-") as temporary:
        root = Path(temporary)
        for index, case in enumerate(selected):
            if case["suite"] == "detection":
                gold = {(m["document"], m["category"], m["start"], m["end"]) for m in case["gold"]["mentions"]}
                inp = DetectionInput(tuple(case["documents"]), tuple(case["policy"]["names"]),
                                     tuple(case["policy"]["organizations"]))
                for number, (name, detector) in enumerate(detectors.items()):
                    work = root / f"d-{index}-{number}"
                    work.mkdir()
                    try:
                        predicted = set(detector.predict(inp, work))
                        for doc, category, start, end in predicted:
                            if type(doc) is not int or not 0 <= doc < len(inp.documents) or not isinstance(category, str):
                                raise ValueError("invalid detector output")
                            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(inp.documents[doc]):
                                raise ValueError("invalid detector location")
                        categories = {m[1] for m in gold | predicted} | {"PERSON", "ORG"}
                        row = {"case": case["id"], "adapter": name, "suite": "detection", "status": "complete",
                               "overall": score(gold, predicted), "by_category": {
                                   cat: score({m for m in gold if m[1] == cat}, {m for m in predicted if m[1] == cat})
                                   for cat in sorted(categories)}}
                    except Exception:
                        row = {"case": case["id"], "adapter": name, "suite": "detection", "status": "failed"}
                        errors.append({"case": case["id"], "adapter": name, "code": "DETECTOR_FAILED"})
                    det_records[name].append(row)
                    case_results.append(row)
                continue
            tables = make_release_input(case["tables"])
            for treatment in TREATMENT_VERSIONS:
                work = root / f"r-{index}-{treatment}"
                work.mkdir()
                status = "complete"
                try:
                    public, alignment = transform(tables, treatment, work)
                    if public is None:
                        status = "rejected"
                    else:
                        utility = task_score(public, case["gold"]["task_rows"], case["gold"]["relations"], alignment)
                except Exception:
                    status = "failed"
                    errors.append({"case": case["id"], "adapter": treatment, "code": "TRANSFORM_FAILED"})
                utility_row = {"status": status, **(utility if status == "complete" else {})}
                utility_records[treatment].append(utility_row)
                if status == "complete":
                    view = AttackView(public, tuple(Knowledge(**k) for k in case["background"]),
                                      tuple(Knowledge(**k) for k in case["history"]))
                    identity_gold = {(alignment[s["subject"]], s["identity"]) for s in case["gold"]["subjects"]}
                    attribute_gold = {(s["identity"], f) for s in case["gold"]["subjects"] for f in s["facts"]}
                    request = request_for(case["id"], treatment, view)
                    if requests is not None:
                        requests.append(request)
                for name in attack_records[treatment]:
                    row = {"case": case["id"], "treatment": treatment, "attack": name,
                           "suite": "attack", "status": status}
                    if status == "complete":
                        try:
                            if name == "pi_agent":
                                response = responses.get(request["request_id"])
                                if response is None:
                                    row["status"] = "missing"
                                    errors.append({"case": case["id"], "adapter": name, "code": "ATTACK_MISSING"})
                                    attack_records[treatment][name].append(row)
                                    case_results.append(row)
                                    continue
                                used_responses.add(request["request_id"])
                                claims = response_claims(response, request)
                                if response["status"] != "complete":
                                    raise ValueError("external attack did not complete")
                            else:
                                claims = attackers[name].run(view)
                                if not isinstance(claims, Claims):
                                    raise ValueError("invalid attacker output")
                            row.update(identity=score(identity_gold, claims.identities),
                                       attribute=score(attribute_gold, claims.attributes))
                        except Exception:
                            row["status"] = "failed"
                            errors.append({"case": case["id"], "adapter": name, "code": "ATTACK_FAILED"})
                    attack_records[treatment][name].append(row)
                    case_results.append(row)
    if responses.keys() - used_responses:
        raise ValueError("responses do not match selected inputs")
    detection = {}
    for name, rows in det_records.items():
        completed = [r for r in rows if r["status"] == "complete"]
        cats = {c for r in completed for c in r["by_category"]}
        detection[name] = {"scheduled": len(rows), "completed": len(completed), "failed": len(rows) - len(completed),
                           "overall": combine([r["overall"] for r in completed]), "by_category": {
                               c: combine([r["by_category"][c] for r in completed if c in r["by_category"]]) for c in sorted(cats)}}
    utilities = {}
    for name, rows in utility_records.items():
        complete = [r for r in rows if r["status"] == "complete"]
        exact = sum(r["exact"] for r in complete)
        utilities[name] = {"scheduled": len(rows), "delivered": len(complete),
                           "rejected": sum(r["status"] == "rejected" for r in rows),
                           "failed": sum(r["status"] == "failed" for r in rows), "exact": exact,
                           "exact_rate": exact / len(rows) if rows else None,
                           "delivered_exact_rate": exact / len(complete) if complete else None,
                           "facts": combine([r["facts"] for r in complete]),
                           "relations": combine([r["relations"] for r in complete])}
    cross_file = None
    if include_cross_file:
        from evals.run_d2 import run as run_d2
        cross_file = run_d2(Path(__file__).resolve().parents[1] / "data/d2-v1.jsonl", split)
        if cross_file["errors"]:
            errors.append({"case": "legacy-d2", "adapter": "d2_graph", "code": "CROSS_FILE_REGRESSION"})
    return {"schema_version": "effectiveness-report-v1", "protocol_version": PROTOCOL_VERSION,
            "dataset": {k: manifest[k] for k in ("version", "sha256", "cases", "families", "synthetic", "scope")},
            "split": split, "case_ids": [c["id"] for c in selected],
            "detector_versions": {name: d.version for name, d in detectors.items()},
            "treatment_versions": TREATMENT_VERSIONS,
            "attack_versions": {name: a.version for name, a in attackers.items()},
            "external_attacker": metadata, "detection": detection,
            "attacks": {t: {n: attack_summary(rows) for n, rows in group.items()} for t, group in attack_records.items()},
            "paired_attack_deltas_vs_raw": {t: {n: paired_attacks(attack_records["raw"][n], rows)
                for n, rows in group.items()} for t, group in attack_records.items() if t != "raw"},
            "utility": utilities, "cross_file_regression": cross_file, "cases": case_results, "errors": errors,
            "environment": {"python": platform.python_version(), "platform": platform.system()},
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "limitations": ["synthetic_exploratory_not_real_world_accuracy", "no_statistical_independence_claim",
                            "candidate_bytes_not_full_delivery_protocol", "no_unknown_background_or_semantic_attack_coverage",
                            "no_human_review_cost_measurement", "no_privacy_certificate"]}

"""Exact metrics: missed, failed, rejected and unavailable are distinct."""


def score(gold, predicted):
    gold, predicted = set(gold), set(predicted)
    tp, fp, fn = len(gold & predicted), len(predicted - gold), len(gold - predicted)
    return counts_score(tp, fp, fn)


def counts_score(tp, fp, fn):
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}


def combine(scores):
    return counts_score(*(sum(s[k] for s in scores) for k in ("tp", "fp", "fn")))


def attack_summary(rows):
    completed = [r for r in rows if r["status"] == "complete"]
    result = {"scheduled": len(rows), "completed": len(completed),
              **{s: sum(r["status"] == s for r in rows) for s in ("failed", "rejected", "missing")}}
    for metric in ("identity", "attribute"):
        values = combine([r[metric] for r in completed])
        values["targets"] = values["tp"] + values["fn"]
        values["success_rate"] = values["recall"]
        result[metric] = values
    result["cases_with_success"] = sum(r["identity"]["tp"] + r["attribute"]["tp"] > 0 for r in completed)
    return result


def paired_attacks(raw, candidate):
    """Match by case; rejected/failed observations cannot improve a risk delta."""
    baseline = {r["case"]: r for r in raw}
    pairs = [(baseline[r["case"]], r) for r in candidate
             if r["case"] in baseline and r["status"] == baseline[r["case"]]["status"] == "complete"]
    result = {"paired_cases": len(pairs), "excluded_cases": len(candidate) - len(pairs)}
    for metric in ("identity", "attribute"):
        before = combine([a[metric] for a, _ in pairs])
        after = combine([b[metric] for _, b in pairs])
        if before["tp"] + before["fn"] != after["tp"] + after["fn"]:
            raise ValueError("paired target mismatch")
        result[metric] = {"before": before["recall"], "after": after["recall"],
                          "delta": after["recall"] - before["recall"]
                          if before["recall"] is not None and after["recall"] is not None else None}
    return result


def compare_reports(current, baseline):
    """Compare only the same inputs, threat model, scorer and adapter set.

    Adapter implementation versions may change: that is the point of comparison.
    This is a deterministic regression gate, not a statistical significance test.
    """
    for key in ("schema_version", "dataset", "split", "case_ids", "protocol_version", "attack_versions"):
        if current.get(key) != baseline.get(key):
            raise ValueError("incompatible comparison")
    if current.get("external_attacker") != baseline.get("external_attacker"):
        raise ValueError("incompatible external attacker")
    if baseline.get("errors") or current.get("errors"):
        raise ValueError("incomplete comparison")
    for section in ("detection", "attacks", "utility"):
        if set(current[section]) != set(baseline[section]):
            raise ValueError("incompatible adapter set")
    changes = []

    def check(path, new, old, higher_is_better=True):
        if new is None or old is None:
            if new != old:
                changes.append({"metric": path, "kind": "availability_changed"})
            return
        if (new < old) if higher_is_better else (new > old):
            changes.append({"metric": path, "before": old, "after": new})

    for name, result in current["detection"].items():
        previous = baseline["detection"][name]
        if set(result["by_category"]) != set(previous["by_category"]):
            raise ValueError("incompatible categories")
        for category, metrics in {"overall": result["overall"], **result["by_category"]}.items():
            old = previous["overall"] if category == "overall" else previous["by_category"][category]
            for field in ("precision", "recall"):
                check(f"detection.{name}.{category}.{field}", metrics[field], old[field])
    for treatment, attacks in current["attacks"].items():
        if set(attacks) != set(baseline["attacks"][treatment]):
            raise ValueError("incompatible attack set")
        for name, result in attacks.items():
            old = baseline["attacks"][treatment][name]
            if any(result[k] != old[k] for k in ("scheduled", "completed", "failed", "rejected", "missing")):
                raise ValueError("incomplete paired attacks")
            for metric in ("identity", "attribute"):
                check(f"attacks.{treatment}.{name}.{metric}", result[metric]["success_rate"],
                      old[metric]["success_rate"], False)
    for name, result in current["utility"].items():
        check(f"utility.{name}", result["exact_rate"], baseline["utility"][name]["exact_rate"])
    cross, old_cross = current.get("cross_file_regression"), baseline.get("cross_file_regression")
    if bool(cross) != bool(old_cross):
        raise ValueError("incompatible cross-file suite")
    if cross:
        if any(cross[k] != old_cross[k] for k in ("dataset", "sha256", "split", "cases")) or cross["errors"] or old_cross["errors"]:
            raise ValueError("incompatible cross-file comparison")
        for metric in ("precision", "recall"):
            check(f"cross_file.link_pairs.{metric}", cross["link_pairs"][metric], old_cross["link_pairs"][metric])
            for rule, values in cross["risk_case_metrics"].items():
                check(f"cross_file.{rule}.{metric}", values[metric], old_cross["risk_case_metrics"][rule][metric])
    return changes

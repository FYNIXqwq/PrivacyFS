"""Offline effect/attack evaluation CLI. No subprocess model or network calls."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from evals.effectiveness.runner import run, PROTOCOL_VERSION
from evals.effectiveness.metrics import compare_reports
from privacyfs.task_profiles import strict_json


def write_new(path, data):
    # Never overwrite a source, frozen corpus or previous report by accident.
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evals/data/effectiveness-v1.jsonl")
    parser.add_argument("--split", choices=("development", "validation", "all"), default="validation")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--export-attack-requests", type=Path)
    parser.add_argument("--pi-responses", type=Path)
    parser.add_argument("--include-cross-file", action="store_true", help="Include the existing frozen D2 graph regression")
    parser.add_argument("--require-no-residual-attacks", action="store_true")
    args = parser.parse_args()
    try:
        requests = [] if args.export_attack_requests else None
        result = run(args.dataset, args.split, requests=requests, responses_path=args.pi_responses,
                     include_cross_file=args.include_cross_file)
        regressions = compare_reports(result, strict_json(args.baseline.read_text(encoding="utf-8"))) if args.baseline else []
        residual = any(a[metric]["tp"] for a in result["attacks"]["privacyfs"].values()
                       for metric in ("identity", "attribute"))
        failed = bool(result["errors"] or regressions or (args.require_no_residual_attacks and residual))
        result["gate"] = {"status": "failed" if failed else "passed",
                          "meaning": "execution_and_explicit_regression_policy_not_privacy_safety",
                          "residual_attacks_observed": residual,
                          "require_no_residual_attacks": args.require_no_residual_attacks,
                          "regressions": regressions}
        if args.export_attack_requests:
            write_new(args.export_attack_requests, {"schema_version": PROTOCOL_VERSION, "requests": requests})
        if args.output:
            write_new(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"status": "failed", "code": "EVALUATION_INPUT_OR_IO_ERROR"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

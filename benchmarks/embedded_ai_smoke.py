"""Explicit real-GGUF smoke test using only newly created synthetic filenames.

Not part of pytest; never downloads a model or scans existing user documents.
"""
import argparse
import json
from pathlib import Path
import time

from privacyfs.gui.backend import ScanController, ScanOptions, export_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    args = parser.parse_args()
    model = args.model.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=False)
    root = args.output / "source"
    for folder, filename in [("software", "LICENSE-MIT"), ("medical", "张三-13812345678.txt")]:
        directory = root / folder
        directory.mkdir(parents=True)
        (directory / filename).write_bytes(b"SYNTHETIC BODY NOT FOR MODEL INPUT")
    controller = ScanController()
    started = time.monotonic()
    last_phase = None
    try:
        controller.start(ScanOptions(str(root.resolve()), directory_ai=True, ai_backend="embedded",
                                     ai_model_path=str(model), ai_batch_items=2, ai_worker_timeout=180))
        while controller.running:
            controller.poll()
            phase = controller.report.get("phase")
            if phase != last_phase:
                print(json.dumps({"phase":phase, "elapsed":round(time.monotonic()-started, 1)}), flush=True)
                last_phase = phase
            time.sleep(0.05)
        export_report(args.output / "report.json", controller.snapshot())
        print(json.dumps({"status":controller.report.get("status"), "reason":controller.report.get("reason"),
                          "stats":controller.report.get("stats"), "model_loads":controller.report.get("ai_model_loads"),
                          "seconds":round(time.monotonic()-started, 2)}), flush=True)
        return 0 if controller.report.get("status") == "complete" else 1
    finally:
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Assemble only reviewed wheels and trial files, never copy a workspace tree."""
from pathlib import Path
from email.parser import BytesParser
import argparse
import hashlib
import json
import shutil
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wheelhouse", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    wheels = output / "wheelhouse"
    wheels.mkdir()
    distributions = {}
    for wheel in sorted(args.wheelhouse.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            members = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA") and n.count("/") == 1]
            if len(members) != 1:
                raise ValueError("invalid wheel metadata")
            metadata = BytesParser().parsebytes(archive.read(members[0]))
            name, version = metadata["Name"], metadata["Version"]
            licenses = [n for n in archive.namelist() if "license" in n.lower() or "copying" in n.lower()]
        target = wheels / wheel.name
        shutil.copyfile(wheel, target)
        key = (name, version)
        record = distributions.setdefault(key, {"hashes": [], "license_files": licenses,
                                               "declared_license": metadata.get("License-Expression") or metadata.get("License")})
        record["hashes"].append(hashlib.sha256(target.read_bytes()).hexdigest())
    app = [(name, version) for name, version in distributions if name.lower() == "privacyfs"]
    if len(app) != 1:
        raise ValueError("exactly one application version required")
    requirements = []
    for (name, version), data in sorted(distributions.items()):
        requirements.append(name + "==" + version + " " + " ".join("--hash=sha256:" + h for h in data["hashes"]))
    (output / "runtime-requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
    shutil.copyfile(root / "packaging/manage.py", output / "manage.py")
    for name in ("TRIAL_D5.md", "FORMATS_D5.md", "PILOT_FEEDBACK_TEMPLATE.md"):
        shutil.copyfile(root / name, output / name)
    (output / "DEPENDENCIES.json").write_text(json.dumps([
        {"name": name, "version": version, "license_files_in_wheel": data["license_files"], "declared_license": data["declared_license"]}
        for (name, version), data in sorted(distributions.items())], ensure_ascii=False, indent=2), encoding="utf-8")
    files = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob("*") if p.is_file()}
    (output / "manifest.json").write_text(json.dumps({"schema_version": "d5-offline-bundle-1", "version": app[0][1],
        "python": ["3.10-x64", "3.11-x64", "3.12-x64"], "files": files,
        "scope": "local_trial_not_a_signed_public_distribution"}, indent=2), encoding="utf-8")
    archive_path = Path(str(output) + ".zip")
    with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output))
    print(json.dumps({"archive": str(archive_path), "version": app[0][1], "wheels": len(list(wheels.iterdir())),
                      "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(), "bytes": archive_path.stat().st_size}))


if __name__ == "__main__":
    main()

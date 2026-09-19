"""Create a versioned synthetic provenance corpus, never overwrite a release.

Known-list/regex localization tests only: not unknown-entity NER ground truth.
"""
from pathlib import Path
import argparse
import hashlib
import json


def entity_cases():
    names = ["plain", "utf16", "traditional", "fullwidth", "phone", "wide_phone",
             "email", "german", "ligature", "combining", "csv_name", "csv_phone",
             "markdown", "csv_multiline", "csv_quotes", "hangul", "dotted_i",
             "gb18030", "negative", "keyword", "negative_columns"]
    for family in names:
        for n in range(48):
            needle, literal, category = "张三", "张三", "NAME_LIST"
            prefix, suffix, extension, encoding = f"record {n}: ", "; end", ".txt", "utf-8"
            rules = {}
            if family == "utf16": encoding = "utf-16-le"
            if family == "traditional": needle = "張三"
            if family == "fullwidth": needle, literal = "Ａｌｉｃｅ", "Alice"
            if family in {"phone", "wide_phone", "csv_phone"}:
                needle = f"138{n:08d}"
                literal = None
                category = "PATTERN"
                rules = {"regexes": [r"(?<!\d)1[3-9]\d{9}(?!\d)"]}
                if family == "wide_phone": needle = "".join(chr(ord(c) + 0xFEE0) for c in needle)
            if family == "email":
                needle, literal, category = f"case{n}@example.invalid", None, "PATTERN"
                rules = {"regexes": [r"[\w.+-]+@[\w-]+\.[A-Za-z]+"]}
            if family == "german": needle, literal = "Straße", "STRASSE"
            if family == "ligature": needle, literal = "oﬃce", "office"
            if family == "combining": needle, literal = "Cafe\u0301", "café"
            if family == "hangul": needle, literal = "\u1100\u1161", "가"
            if family == "dotted_i": needle, literal = "İpek", "i\u0307pek"
            if family == "keyword":
                needle, literal, category = "計算機專業", None, "KEYWORD"
                rules = {"keywords": ["计算机专业"]}
            if family == "markdown":
                prefix, suffix, extension = f"# Example {n}\n```text\n客户：", "\n```\n", ".md"
            if family in {"csv_name", "csv_phone", "csv_multiline", "csv_quotes", "gb18030"}:
                extension = ".csv"
                prefix, suffix = f"id,value\r\n{n:04d},\"", '"\r\n'
                if family == "csv_multiline": prefix += "first line\r\n"
                if family == "csv_quotes": needle, literal = 'a""b', 'a"b'
                if family == "gb18030": encoding = "gb18030"
            if literal is not None: rules = {"literal_names": [literal]}
            text = prefix + needle + suffix
            expected = [{"start": len(prefix), "end": len(prefix) + len(needle), "category": category}]
            if family == "negative":
                text, expected = f"Nothing identifying in synthetic example {n}.", []
            if family == "negative_columns":
                text, extension, expected = f"id,left,right\n{n},张,三\n", ".csv", []
            # Encoding, script and entity substitutions inside the same record
            # wrapper are one template family, not separate train/test groups.
            if family == "negative":
                group, split = "entity-negative", "development"
            elif family == "negative_columns":
                group, split = "entity-csv-negative-columns", "validation"
            elif extension == ".csv":
                group, split = "entity-csv-value", "validation"
            elif family == "markdown":
                group, split = "entity-markdown", "validation"
            else:
                group, split = "entity-record", "development"
            yield {
                "id": f"entity-{family}-{n:03d}", "kind": "entity", "group": group,
                "variant_family": family, "split": split, "rules": rules,
                "documents": [{"path": "sample" + extension, "text": text, "encoding": encoding,
                               "bom": encoding.startswith("utf-16"),
                               "explicit_encoding": "gb18030" if encoding == "gb18030" else None,
                               "status": "complete", "findings": expected}],
            }


def group_cases():
    for family in ["same_org", "same_name", "duplicate", "bridge", "malformed", "unsupported"]:
        for n in range(10):
            rules = {"literal_names": ["张三"]}
            documents = []
            if family in {"same_name", "duplicate", "same_org"}:
                value = "某某公司" if family == "same_org" else "张三"
                category = "ORG" if family == "same_org" else "NAME_LIST"
                if family == "same_org": rules = {"literal_orgs": [value]}
                for index in range(2):
                    prefix = f"example {n}: " if family == "duplicate" else f"context {n}/{index}: "
                    documents.append({"path": f"file{index}.txt", "text": prefix + value,
                        "status": "complete", "findings": [{"start": len(prefix), "end": len(prefix) + len(value), "category": category}]})
            elif family == "bridge":
                values = [f"id,name\nC{n},张三\n", f"id,meeting\nC{n},M{n}\n", f"meeting M{n}: pending review"]
                for i, value in enumerate(values):
                    at = value.find("张三")
                    documents.append({"path": f"file{i}" + (".csv" if i < 2 else ".md"), "text": value,
                        "status": "complete", "findings": [] if at < 0 else [{"start": at, "end": at + 2, "category": "NAME_LIST"}]})
            else:
                documents = [{"path": "sample.csv" if family == "malformed" else "sample.pdf",
                    "text": 'a,b\n"unfinished' if family == "malformed" else "unsupported synthetic PDF",
                    "status": "failed" if family == "malformed" else "unsupported", "findings": []}]
            yield {"id": f"group-{family}-{n}", "kind": "file_group", "group": f"group-{family}",
                "split": "validation" if family in {"duplicate", "malformed", "unsupported"} else "development",
                "rules": rules, "documents": documents,
                "future_d2_scenario": family, "d1_scores_relationships": False}


def task_cases():
    for family in ["leading_zeroes", "quotes", "ragged", "code_literal"]:
        for n in range(5):
            if family == "leading_zeroes":
                text, rows = f"id,amount\n{n:04d},0012\n", [["id", "amount"], [f"{n:04d}", "0012"]]
            elif family == "quotes":
                text, rows = 'id,note\r\n001,"first\nsecond ""quote"""\r\n', [["id", "note"], ["001", 'first\nsecond "quote"']]
            elif family == "ragged":
                text, rows = "a,b\nx\n\nz,w,q\n", [["a", "b"], ["x"], [], ["z", "w", "q"]]
            else:
                text, rows = f"# Sample {n}\n```python\nprint('never execute this')\n```\n", None
            yield {"id": f"task-{family}-{n}", "kind": "task_preservation", "group": f"task-{family}",
                "split": "validation" if family in {"ragged", "code_literal"} else "development", "rules": {},
                "documents": [{"path": "sample.md" if rows is None else "sample.csv", "text": text,
                               "status": "complete", "findings": [], "expected_rows": rows}],
                "d1_scores_task_reasoning": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="d1-v2")
    args = parser.parse_args()
    if not args.version.replace("-", "").isalnum(): parser.error("invalid version name")
    base = Path(__file__).parent / "data"
    base.mkdir(exist_ok=True)
    target = base / f"{args.version}.jsonl"
    manifest = base / f"{args.version}.manifest.json"
    if target.exists() or manifest.exists():
        raise SystemExit("Frozen version exists. Choose a new version after reviewing changes.")
    cases = [*entity_cases(), *group_cases(), *task_cases()]
    payload = "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases).encode("utf-8")
    target.write_bytes(payload)
    data = {"version": args.version, "sha256": hashlib.sha256(payload).hexdigest(), "synthetic": True,
            "counts": {kind: sum(c["kind"] == kind for c in cases) for kind in ("entity", "file_group", "task_preservation")},
            "scope": "Known-list/regex localization and parsing, not NER accuracy, inference or task reasoning.",
            "split_policy": "Related record/CSV wrappers, including encoding and script variants, share one split; normalized duplicate inputs cannot cross splits."}
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()

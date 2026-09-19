"""Validate the authored fixture, not general product behavior or model accuracy."""
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "examples/enterprise-sim-v1"
SOURCE = ROOT / "workspace"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))
from privacyfs.documents import DocumentSession
from privacyfs.config import load_rules
from privacyfs.relations import load_relation_config
from privacyfs.evidence import EvidenceGraph
from privacyfs.task_profiles import load_task_profile
from privacyfs.planning import build_plan
from privacyfs.verifier import verify_candidate
from evals.effectiveness.runner import run as run_effectiveness


def rows(relative):
    with (SOURCE / relative).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
expected_files = {f["path"] for f in manifest["files"]}
assert expected_files == {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file() and p != ROOT / "manifest.json"}
before = {}
for entry in manifest["files"]:
    path = ROOT / entry["path"]
    payload = path.read_bytes()
    before[entry["path"]] = hashlib.sha256(payload).hexdigest()
    assert before[entry["path"]] == entry["sha256"] and len(payload) == entry["bytes"]
    if path.suffix == ".csv":
        parsed = list(csv.reader(io.StringIO(payload.decode("utf-8"), newline="")))
        assert parsed and len(set(parsed[0])) == len(parsed[0])
        assert all(len(r) == len(parsed[0]) for r in parsed[1:]), entry["path"]
    elif path.suffix == ".json":
        json.loads(payload)
    elif path.suffix == ".jsonl":
        for line in payload.decode("utf-8").splitlines():
            json.loads(line)

truth = json.loads((ROOT / "evaluator/ground_truth.json").read_text(encoding="utf-8"))
for assertion in truth["assertions"]:
    assert assertion["expected"] in {"supported", "unsupported"}
    for evidence in assertion["sources"]:
        assert evidence["contains"] in (ROOT / evidence["path"]).read_text(encoding="utf-8"), assertion["id"]

staff = rows("01_人力资源/员工花名册_2026-08.csv")
employee_ids = {r["员工编号"] for r in staff}
assert len(staff) == len(employee_ids) == 12
assert sum(r["姓名"] == "陈宇航" for r in staff) == 2
clients = rows("02_客户与商务/客户主数据.csv")
client_ids = {r["客户编号"] for r in clients}
projects = rows("03_项目交付/项目台账.csv")
project_ids = {r["项目编号"] for r in projects}
assert len(client_ids) == 4 and len(project_ids) == 5
assert sum(p["客户编号"] == "INTERNAL" for p in projects) == 1
for p in projects:
    assert p["客户编号"] in client_ids | {"INTERNAL"} and p["负责人工号"] in employee_ids
allocation = defaultdict(float)
for row in rows("03_项目交付/项目成员.csv"):
    assert row["项目编号"] in project_ids and row["员工编号"] in employee_ids
    allocation[row["员工编号"]] += float(row["本期投入比例"])
assert all(v <= 1.00001 for v in allocation.values())
salary = rows("01_人力资源/薪酬/2026-08_薪酬明细.csv")
assert {r["员工编号"] for r in salary} == employee_ids
for r in salary:
    assert sum(int(r[k]) for k in ("基本工资", "固定补贴", "本期奖金")) == int(r["税前应发"])
assert sum(int(r["税前应发"]) for r in salary) == truth["august_gross_pay"]
costs = rows("04_财务/项目工时成本_2026-08.csv")
assert all(int(r["核算工时"])*int(r["内部标准小时费率"]) == int(r["标准分摊成本"]) for r in costs)
assert sum(int(r["标准分摊成本"]) for r in costs) == truth["standard_project_cost"]
assert Counter(r["部门"] for r in staff) == {r["部门"]:int(r["人数"]) for r in rows("00_公司运营/组织与岗位.csv")}
for path in SOURCE.rglob("*.csv"):
    for r in rows(path.relative_to(SOURCE)):
        for key in ("员工编号", "负责人工号", "主管工号", "审批工号", "主持工号", "处理工号"):
            if key in r:
                assert r[key] in employee_ids, (path.name,key)
        if "项目编号" in r:
            assert r["项目编号"] in project_ids
        if "客户编号" in r:
            assert r["客户编号"] in client_ids | {"INTERNAL"}
        for key in ("工作邮箱", "联系邮箱"):
            if key in r:
                assert r[key].endswith(".example")
        for key in ("模拟联系电话", "模拟收款账户"):
            if key in r:
                assert r[key].startswith("SIM-")

rule_config = load_rules(ROOT / "controls/rules.synthetic.yaml")
assert rule_config.ner_engine == "off"
configuration_results = {}
for path in sorted((ROOT / "controls").glob("relations-*.json")):
    config = load_relation_config(path)
    with DocumentSession(SOURCE) as session:
        bindings = [(session.parse(session.capture(p), encoding=enc), t) for p,t,enc in config]
        with EvidenceGraph(bindings) as graph:
            quasi = ("部门", "工作城市", "岗位") if "employee-audit" in path.name else ()
            analysis = graph.analyze(quasi_fields=quasi)
            # Same visible name must not collapse different exact employee IDs.
            people = {graph.resolver.assignments[oid] for oid,o in graph.resolver.occurrences.items()
                      if o.namespace == "employees"}
            if "employee" in path.name or "enterprise" in path.name:
                assert len(people) == 12
            configuration_results[path.name] = {"bound_files":len(bindings),"risks":dict(Counter(w.rule for w in analysis.witnesses))}

plans = {}
for profile_name, relation_name in (("profile-commercial-minimal.json","relations-commercial.json"),
                                    ("profile-commercial-context.json","relations-commercial.json"),
                                    ("profile-employee-bands.json","relations-employee-release.json")):
    profile = load_task_profile(ROOT / "controls" / profile_name)
    config = load_relation_config(ROOT / "controls" / relation_name)
    with DocumentSession(SOURCE) as session:
        bindings = [(session.parse(session.capture(p), encoding=enc), t) for p,t,enc in config]
        with EvidenceGraph(bindings) as graph:
            plan = build_plan(bindings, graph, graph.analyze(), profile)
            assert plan.selected is not None, profile_name
            candidate = plan.candidates[plan.selected]
            with tempfile.TemporaryDirectory(prefix="enterprise-sim-candidate-") as temporary:
                actual, emitted = [], []
                for a in candidate.artifacts:
                    path = Path(temporary) / a.filename
                    path.write_bytes(a.payload)
                    actual.append(replace(a, payload=path.read_bytes()))
                    emitted.extend(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))
                verify_candidate(plan, replace(candidate, artifacts=tuple(actual)))
                payload = b"".join(a.payload for a in actual).decode("utf-8")
                assert all(r["企业名称"] not in payload for r in clients)
                assert all(r["姓名"] not in payload for r in staff)
                if "commercial" in profile_name:
                    assert Counter(r["status"] for r in emitted if "status" in r) == truth["latest_external_status_counts"]
                    if "minimal" in profile_name:
                        assert all(p["业务主题"] not in payload for p in projects)
                else:
                    assert len([r for r in emitted if "pay_band" in r]) == 12
                    assert {r["pay_band"] for r in emitted if "pay_band" in r} <= {"20000-29999","30000-39999","40000-49999"}
                plans[profile_name] = {"verified_artifacts":len(actual),"emitted_rows":len(emitted),"original_identities_absent":True}

env = dict(os.environ, PYTHONIOENCODING="utf-8")
process = subprocess.run([str(REPO / ".venv/Scripts/privacyfs.exe"), "relate", str(SOURCE), "--config",
                          str(ROOT / "controls/relations-commercial.json")], capture_output=True, text=True, encoding="utf-8",env=env)
assert process.returncode == 0, process.returncode
public = json.loads(process.stdout)
assert all(r["企业名称"] not in process.stdout for r in clients)
effectiveness = run_effectiveness(ROOT / "evaluator/ef1-commercial-v1.jsonl", "all")
assert effectiveness["errors"] == []
assert effectiveness["utility"]["privacyfs"]["exact_rate"] == 1
assert effectiveness["attacks"]["privacyfs"]["background_linkage"]["identity"]["tp"] == 2
assert effectiveness["attacks"]["privacyfs"]["history_linkage"]["identity"]["tp"] == 4
for name,digest in before.items():
    assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
result = {"status":"passed","fixture":"enterprise-sim-v1","files":len(before)+1,"workspace_files":43,
          "assertions_checked":len(truth["assertions"]),"employee_count":12,"external_clients":4,"projects":5,
          "august_gross_pay":truth["august_gross_pay"],"standard_project_cost":truth["standard_project_cost"],
          "configs":configuration_results,"plans":plans,"cli_relate_exit":process.returncode,
          "ef1_slice":{"cases":1,"privacyfs_task_exact":True,"background_identity_hits":2,"history_identity_hits":4},
          "source_and_fixture_hashes_unchanged":True,"real_model_executed":False}
with (REPO / ".review/enterprise-sim-v1-validation-final.json").open("x",encoding="utf-8") as stream:
    json.dump(result,stream,ensure_ascii=False,indent=2)
print(json.dumps(result,ensure_ascii=False,indent=2))

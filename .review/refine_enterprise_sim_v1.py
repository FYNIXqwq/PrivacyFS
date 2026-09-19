"""Initial authoring review before delivery; no legacy/frozen eval corpus edits."""
from pathlib import Path
import hashlib
import json

repo = Path(__file__).resolve().parents[1]
root = repo / "examples/enterprise-sim-v1"
manifest_path = root / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
for entry in manifest["files"]:
    assert hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
changes = {
    "workspace/03_项目交付/P2601_星港/周报_2026-W35.md": [
        ("E1003负责回补接口，E1004检查温度采样缺失；两人共享项目不代表共享人事状态。",
         "E1003负责回补接口，E1004检查温度采样缺失；周可宁汇总两项问题，随下一轮联调一起复核。")],
    "workspace/01_人力资源/例外审批/工位调整申请.md": [
        ("申请文档没有姓名；工位编号可以在资产领用表中找到对应工号。",
         "请资产管理员核对工位安排后与排班负责人确认，下周一前回复处理结果。")],
    "workspace/07_普通业务/培训计划_九月.md": [
        ("；“张力”在这里是物理量。", "，请提前阅读采样说明。"),
        ("；“高峰”表示流量时段。", "，用最近一次压测记录练习。"),
        ("白榆会议室与白榆项目恰好同名。采购白榆木色桌面不构成任何客户项目进展。",
         "会议室白榆的桌面材料确认为白榆木色，物业预计9月1日前安装。培训均使用青松会议室。")],
}
for project in ("P2601_星港", "P2602_白榆", "P2603_砾石", "P2604_归帆"):
    changes[f"workspace/03_项目交付/{project}/交接记录.md"] = [
        ("摘要中保留此完整措辞有助于理解任务，但它并不是匿名编号。",
         "客户周会仍沿用该名称，日报标题请保持一致，避免与其他工作包混淆。")]
builder = repo / ".review/build_enterprise_sim_v1.py"
builder_text = builder.read_text(encoding="utf-8")
for name, replacements in changes.items():
    path = root / name
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text, name
        text = text.replace(old, new)
        builder_text = builder_text.replace(old, new)
    path.write_text(text, encoding="utf-8", newline="")
truth_path = root / "evaluator/ground_truth.json"
truth = json.loads(truth_path.read_text(encoding="utf-8"))
for case in truth["assertions"]:
    if case["id"] == "N06":
        case["sources"][1]["contains"] = "会议室白榆的桌面材料确认为白榆木色"
builder_text = builder_text.replace('"不构成任何客户项目进展")],', '"会议室白榆的桌面材料确认为白榆木色")],')
builder.write_text(builder_text, encoding="utf-8", newline="")
truth_path.write_text(json.dumps(truth,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="")
for entry in manifest["files"]:
    path = root / entry["path"]
    entry["bytes"] = path.stat().st_size
    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
manifest["authoring_note"] = "Before first delivery, instructional prose was replaced with operational wording; no truth labels or legacy benchmark files were changed."
manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="")
print(json.dumps({"reviewed_source_documents":len(changes),"truth_labels_changed":False}))

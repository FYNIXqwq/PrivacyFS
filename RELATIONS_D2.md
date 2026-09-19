# D2：显式关系、实体纠正与来源证据

D2 为指定的 TXT、Markdown、CSV 建立工作区内的实体和事实关系，输出编号连接、身份映射、准标识符组合三类风险。实现以用户声明的字段和关系为前提，不自动理解任意正文，不判断法律合规，也不生成已脱敏副本。旧 `scan/analyze/mirror/tui` 的目录策略继续保留；正文关联由独立 `relate` 命令启用。

## 运行合成示例

从仓库根目录执行：

```powershell
.\.venv\Scripts\privacyfs.exe relate examples/d2 --config examples/d2/relations.json
```

示例的三份文件分别提供“客户编号→企业名称”“客户编号→会议编号”“会议编号→合作状态”。输出应包含一项 `identity_mapping` 和一项 `numbered_join`，其中编号连接是逐文件基线遗漏的。输出为完整 JSON；无原始路径、字段名称、实体值或内容摘要。ID 每次会话随机隔离，不能用来跨运行追踪同一人。

命令参数：

| 参数 | 含义 |
|---|---|
| `ROOT` | 本地项目根目录；绑定的文件路径必须位于根内 |
| `--config FILE` | `d2-templates-1` JSON 配置，最大 1 MiB |
| `--quasi-field NAME` | 重复至少两次，选择已配置的 quasi 字段；默认不运行组合规则 |
| `--max-support N` | 组合规则的建模群组支持度上限，默认 2 |
| `--max-seconds N` | 每个读取、解析、图处理阶段的时间上限，默认 10 秒；不是全命令总耗时 |

退出码：0 表示已完成声明范围内的处理，即使存在风险；2 表示配置或参数错误；4 表示读取、解析、模板、取消或资源限额失败。处理失败返回 `status=failed`、`risk_count=null`。某些早期文件系统/配置错误仅有安全 stderr，不保证所有非零退出都有 JSON。`privacy_verified` 始终为 false。处理完整且没有已识别风险也不代表整个文件可安全发布。

## 模板与字段语义

每个 file binding 包含相对 `path`、`primary`、`fields`，可选 `encoding`、`state_field`、`default_state`。不递归发现文件，不读取配置外的正文。重复 JSON 键、未知设置、无主键、不合法字段、未声明的敏感值词表均拒绝。

| role | 语义 |
|---|---|
| key | 显式编号，必须有 namespace 和合法 entity_type |
| identity | 把主键关联到身份名称；该名称不会自动合并不同编号 |
| sensitive | 模板声明的敏感属性，必须有非空 values 词表；词表外值记录为 unknown |
| quasi | 潜在准标识符属性，仅在显式选择该字段组合时参与规则 |
| label | 普通名称候选；不直接生成身份映射风险 |

主键必须是 key。同一记录的其他 key 形成**主键→引用键**的有向关系。这是配置者声明的关系，不是算法从同一行的共现猜出来的。同一机构下多个人不会因此被合并，也不会自动产生机构→所有员工的逆向边。

实体类型包括 `person`、`organization`、`client`、`project`、`event`。自动合并仅使用完全相同的 `(entity_type, namespace, value)`；编号保留前导零、空格、大小写和 Unicode 原字形。`001` 与 `1`、全角与半角编号不会自动视为相同。已知规则需要兼容编号形式时，应在输入规范或有版本的人工纠正中明确处理，不能把名称规范化直接作为主键共指。

不同编号的 identity/label 在同类型、同 namespace 内规范化后相同，会形成 `candidate` 组。候选不会成为确认关系；可以拒绝候选而保留双方实体和事实。人物与企业名称不跨类型匹配。首版不做编辑距离、拼音、NER 或模型共指。

CSV 必须有明确且不重复的表头；按字段名称映射列，行长必须与表头一致。额外列不进入图，报告 scope 始终是 `explicit_configured_fields`。空主键失败，空可选属性不生成事实。CSV 数值不自动转型，公式不执行。字段语义不会越过行边界。

TXT/Markdown 只支持如下精确字面格式，每个非空行一个记录：

```text
会议编号=M001;合作状态=暂停洽谈
```

字段顺序可变化，但名称必须完全匹配，不能缺少、重复或多出字段。不支持值中的分号、任意自然语言段落或 Markdown 表格；需要这些内容时使用 CSV 或等待后续模板扩展。以 `>` 开头的记录标为 quoted。换行与来源位置仍由 D1 保留。

`state_field` 可指定状态列，值为 `positive/negative/quoted/unknown`；其他值和空状态为 unknown。未指定状态列时使用 `default_state`（默认 positive），这是配置的“字段赋值属于肯定断言”假设。不是一般否定句理解：“不是暂停洽谈”不在敏感值词表时是 unknown。遇到同一建模事实的正反断言同时存在，报告冲突数量，该事实不参与肯定风险通路；不裁定哪份材料为真。unknown、quoted、negative 保留来源但不会伪装成确定事实。

## 风险、重复证据与切断点

| 规则 | 触发与证据 | 限制 |
|---|---|---|
| identity_mapping | 显式编号与身份名称的来源断言 | 表示代号可能被映射回名称，不表示名称已由独立来源核实 |
| numbered_join | 身份断言，经零条或多条有向引用，到达敏感断言 | 同文件也可触发；跨文件增量通过限制可见来源的基线比较 |
| quasi_combination | 至少两个指定属性形成低支持度组合 | 只在已建模、具有完整且无冲突属性的同类型/namespace 主体中计数；不是现实人口匿名集合 |

同一实体的相同事实合并来源，复制文件或重复行不增加主体数，也不获得额外置信度。编号关系和身份映射不因列名改写而多算；quasi/sensitive 字段名称仍定义属性语义。不同时间的矛盾值暂不推断时序：准标识符同字段有冲突值时，该主体不进入完整属性群组。

编号风险用广度优先搜索取得一条最短有向证据通路；循环不会无限遍历。它是最短边数通路，**不是全局最小披露集或最少业务损失方案**。多个同长通路选择一条，其余通路会参与切断检查。

每个 `cuts` 项假设移除该整组建模证据及全部副本。`breaks_witness` 表示当前通路被打断；`blocks_endpoint_route` 只表示该项身份事实到该敏感端点的其他通路也被阻断。它不会宣称其他身份事实、风险或未知外部背景都已消失。切断身份/端点事实针对当前事实对成立；另一身份映射仍可能形成另一个 witness。这里只提供图上的候选切断点，**不等于删除某个原文字符或真实文件就已安全**。实际变换、残留验证与任务损失约束属于 D3。

基线按文件分别限制可见证据，并保持当前实体修订固定；revision 0 时仅含显式编号合并。人工跨文件确认后的基线不是独立重新识别实验。公开增量数字仅针对 numbered_join；准标识符规则在 SDK 基线中可显式传入相同组合做比较。

规则版本为 `d2-explicit-graph-v1`，攻击假设版本为 `d2-directed-key-linkage-v1`：假定接收方能取得所分析的配置字段并按显式键连接。不估计概率，不建模外部辅助数据、未知语义或任意攻击者。

## 本地 SDK 与纠正

```python
from pathlib import Path
from privacyfs.documents import DocumentSession
from privacyfs.relations import load_relation_config
from privacyfs.evidence import EvidenceGraph, public_graph_report

root = Path("examples/d2")
with DocumentSession(root) as session:
    bindings = [
        (session.parse(session.capture(path), encoding=encoding), template)
        for path, template, encoding
        in load_relation_config(root / "relations.json")
    ]
    with EvidenceGraph(bindings) as graph:
        analysis = graph.analyze()
        safe = public_graph_report(graph, analysis)
        witness = next(w for w in analysis.witnesses if w.rule == "numbered_join")
        # 私有本地详情：含原文及定位，不发送给远程模型或普通日志。
        detail = graph.local_evidence(analysis, witness.evidence_ids[1])
        graph.reject_evidence(analysis, witness.evidence_ids[1])
        updated = graph.analyze()  # 旧 analysis 此时已失效
```

`local_evidence` 回放每份来源的 SourceLocator，返回原文覆盖区间、参与的 occurrence ID 以及纠正记录。CSV 引号转义的覆盖区间可能与逻辑值不同；见 D1 位置契约。调用方只能在相应 DocumentSession 与图仍有效时读取；图对象不是安全隔离容器，使用方必须保护其原文和关系。

纠正接口：

- `graph.resolver.confirm(entity_ids, workspace_id=..., expected_version=...)`：确认单实体或合并同类型、同 namespace 的实体。
- `graph.resolver.split(entity_id, occurrence_ids, ...)`：把该实体部分出现位置拆成新实体；同一记录主键的所有相关事实随它移动。
- `graph.resolver.reject_entity(entity_id, ...)`：从活动证据中排除该建模实体；`confirm([id], ...)` 可重新确认。
- `graph.reject_candidate(analysis, candidate_id)`：拒绝名称共指候选，保留实体和事实。
- `graph.reject_evidence(analysis, evidence_id)`：排除所选事实的全部已捕获副本。

纠正必须符合 workspace 和 expected_version，成功后一次递增版本并记录 action、引用和前后版本。不合法操作不部分生效。拒绝事实与候选由当前 analysis 绑定范围。纠正后应重新 analyze，旧报告和旧详情调用抛出 `StaleAnalysisError`。源码更新需要新快照、新图；旧图分析的是已捕获版本，不会自动跟踪磁盘变化。

首版使用会话内存，**不迁移或写入旧 mapping.db，不保存图或纠正到磁盘**。关闭后不能用 CLI 的旧 ID 重新打开证据；CLI 是一次性安全摘要，本地回放和纠正使用上述活跃 SDK 会话。持久索引、撤销历史、交互审核和增量更新留给 D4。保留先前 analysis/detail 的调用方仍可能持有敏感副本；close 不能安全擦除所有 Python 引用、换页或转储。

## 限额与失败边界

D1 原有快照、字符、文件数、解析限制继续生效。图默认最多 10,000 条记录、30,000 条原始断言、30,000 个编号出现位置、1,000 个 witness、200,000 步工作检查、6 条关系深度。普通属性最多 65,536 码点，编号最多 1,024 码点；每模板最多 32 个字段、最多 8 个组合属性。SDK 可通过 `GraphLimits` 调整，继承的文档超时和取消不能被放宽。

D2需要同时保留多文件快照。默认16,000,000保留码点预算会累计原文、块和规范化视图，可能在50 MiB输入之前拒绝；它与D1 inspect逐文件释放的行为不同。大集合SDK基准显式提高该预算，不能据此宣称默认CLI可分析任意50 MiB集合。

任何选定文件解析不完整、模板不匹配、空编号、超限或取消，都不返回一份“零风险成功”的部分图。达到图深度时也明确失败。图与会话为单线程 API；仅取消事件可由外部线程设置。不承诺恶意同机用户竞争下的原子文件捕获、系统级访问隔离、运行时拦截或掉电事务。

## 验证与证据

快速回归与冻结验证：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cross_file_risk.py tests/test_entity_resolution.py tests/test_evidence.py -q
.\.venv\Scripts\python.exe evals/run_d2.py --split validation
.\.venv\Scripts\python.exe benchmarks/evidence_d2.py
```

阶段实施记录仅本地保存；可通过上述测试与评测命令复验。全部数据为合成材料，没有上传用户数据或调用真实模型。

# 效果与攻击评测体系 EF1

2026-09-07，开发工具版本 `effectiveness-report-v1`。这是 D5 后独立的评测工作，不是 D6 运行时隔离。产品检测、策略与已交付安装包未因本任务升级。

## 目的和默认边界

把四件事分开测量：检测有没有找到黄金标注；跨文件图有没有建立正确关系；攻击者能否从可见结果恢复身份或敏感事实；输出是否保留任务事实与关系。

默认只读取明确选择的合成 JSONL 数据集，在新临时目录中运行，不扫描业务目录，不调用模型、读取 Pi 凭据或访问网络。报告不包含原文、身份猜测、私有映射、源文件路径或异常正文。攻击请求文件包含可见的合成内容和声明的先验，不能把它当作可自动分享的无内容诊断包。

这不是通用隐私认证、独立现实盲测或 AI 防御成功率。当前本地攻击是确定性的结构化推断，没有尝试任意自然语言语义、互联网检索、OCR、文件顺序侧信道或未知背景知识。失败、缺失响应、拒绝与有效弃答分别计数。

## 快速运行

在仓库根目录，使用已有隔离环境。输出文件必须尚不存在，防止误覆盖数据或历史证据：

```powershell
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split validation
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split all --include-cross-file --output .review/ef1-report.json
```

`--include-cross-file` 纳入现有 D2 冻结集上的实体连接对、三类规则和逐文件增量回归，保留其原数据哈希与范围说明。它不把 D2 合成满分解释成任意正文的识别精度。D1/D3/D4/D5 原 runner 继续可独立运行，旧语料不修改。

退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 评测执行完整，满足本次显式门禁；可以同时存在漏检、误报和残余攻击 |
| 1 | 检测/变换/攻击执行故障、缺失响应、相对基线退化，或显式要求的无残余攻击门禁失败 |
| 2 | 数据、协议、基线不兼容、输入/输出错误或用户中断；只返回固定错误码 |

开发挑战集有意包括现有能力不支持的场景，因此不把得分低本身设为默认程序故障。显式检查残余攻击：

```powershell
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split validation --require-no-residual-attacks
```

当前版本该命令应退出 1。它检查成功生成的 PrivacyFS 候选上的已实现攻击；即使未来通过，也只说明这些攻击未成功。

## 数据、标注与分区

`evals/data/effectiveness-v1.jsonl` 共 22 个小型合成场景：12 个直接实体检测、10 个三表发布任务，19 个作者指定场景族。development/validation 各 11 个场景。发布任务共用一种客户→会议→事实的结构，不能称为 10 种独立拓扑或真实项目。分区适合工程回归与扩展，不构成未见现实分布上的盲测证据。

检测标注包含 PERSON/ORG 的精确 Unicode 码点位置。名单正例、未知实体、全角、繁体、被引用的人名、普通词与人名同形的负例均有覆盖。引用中出现真实身份仍属于实体检测目标；这和把引用当作肯定敏感事实是两个问题。

发布场景包含无背景、公开编号、独特主题、错误背景、歧义背景、重复知识、历史桥接、否定/引用/未知断言。黄金身份、肯定事实、任务行和主体关系在数据中预先冻结；评分不根据产品或攻击输出生成答案。

manifest 记录 SHA-256、作者来源和范围。loader 检查字节哈希、case 唯一性、整族分区、规范化后相同文档不跨分区、位置回放和字段约束。近重复语义和族划分仍需人工审查，不能用字符串哈希替代。生成器拒绝覆盖已有版本；标签修改必须创建新版本并解释差异。

当前 CLI 仅接受标记为 synthetic 的有限数据协议。这是使用约定，不是对数据真实性的密码学证明。接入真实材料需要另行落实授权、私有存储、报告与原文保留边界；不能仅改 manifest 来自动获得外发授权。

## 适配器和指标

### 直接检测

`DetectionInput` 仅含文本和显式名单，无黄金答案。当前两个适配器：`rules` 使用产品的 RuleInspector，但清空默认关键词/正则/职业与身份词，以便单独测给定名单的 PERSON/ORG 能力；`abstain` 永远弃答，作为校准对照。

每个发现按 `(文档索引, 类别, 起点, 终点)` 精确匹配；重复发现去重，类别错或位置错同时产生 FP/FN。分别报告整体及类别 TP/FP/FN、precision/recall/F1。没有预测时 precision 为 null；没有目标时 recall 为 null。失败适配器不被转换成“零命中”，报告完成率并触发执行门禁。

Python 扩展方式：实现带 `version` 的 `predict(DetectionInput, 临时目录)`，返回位置元组集合，再通过 `run(..., detectors={...})` 注入。扩展是可信开发代码，当前没有任意插件代码的权限沙箱。默认不增加动态导入或模型下载。

### 四种处理方式

| 名称 | 实际行为 |
|---|---|
| raw | 原始合成表值 |
| identity_only | 只清空 name，保留原编号与其余值；是明确的弱对照，不代表竞品 |
| privacyfs | 真实 DocumentSession→EvidenceGraph→build_plan→写入新文件→读回→verify_candidate；对外视图仅从读回 CSV 构造 |
| suppress_all | 空可见表；保留原任务目标作分母，不能凭空输出获得高效用 |

产品策略保留批准的 topic 标签和 fact。攻击者背景不进入产品策略，故能测出批准值与外部资料组合后的风险。全部抑制对照不是产品可行计划，不参与产品策略选择。

仅在这个合成评测适配器中使用可复现 seed，保证外部请求能在重跑时核对；产品发布默认仍随机。私有主体对齐依赖本评测固定行保留契约及 document_index，在评分侧使用，绝不传给攻击者。若将来允许重排、聚合或跨行变换，必须升级对齐协议和 scorer，不能继续按行 zip。

### 攻击者可见范围

`AttackView` 只有 public tables、background、history。没有源路径、黄金答案、源→化名映射、TaskProfile、plan 对象或 release seed。Python 攻击模块不导入 PrivacyFS 的图/检测器，依据公开列语义独立推断。

| 攻击 | 知识与行为 |
|---|---|
| direct_identity | 当前可见客户表中的 name→cid |
| key_join | 在上述映射上经 cid→mid→fact 推断肯定事实 |
| background_linkage | 加上声明的公开编号或主题→身份资料；只有唯一候选时猜测 |
| history_linkage | 再加入显式历史 key→topic，组合成主题→身份再匹配当前结果 |

这些攻击是嵌套能力，报告不可相加。重复背景不增加候选支持；歧义则弃答；错误背景会产生真实 FP，而不是被评分器当作成功。敏感事实仅对 positive 路径推断。当前攻击不处理所有冲突或自由文本语义，有限攻击失败不能代表强攻击失败。

身份评分单位为 `(当前可见主体标识, 真实身份)`；属性评分单位为 `(真实身份, 肯定敏感事实)`。分母是黄金目标，错猜计 FP，缺失计 FN，成功率为 TP/(TP+FN)，同时展示 precision。否定、引用、未知案例没有肯定事实目标，错误的肯定猜测仍计 FP。

攻击完成率、失败、缺失、产品拒绝单独展示。攻击率只对成功执行的样本计算；相对 raw 的差值只纳入两侧均完成的同一 case，报告排除数，不能用拒绝提升差值。没有运行成功的攻击，成功率为 null。

### 任务效用

使用独立黄金多重集合核对 topic、fact、assertion_state，保留重复行数量；同时核对事实属于哪个主体及实际桥接关系。只保留计数、但把事实分配给错误主体不能通过。

报告总任务数、生成候选数、拒绝/失败数、精确完成数、全任务完成率及已生成候选上的完成率。未知断言当前被产品拒绝，计入可交付性损失，不能以删除该案例获得 100%。不测真人审核时间或模型摘要质量。

## 与历史报告比较

```powershell
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split all --include-cross-file --baseline .review/ef1-report.json --output .review/ef1-next.json
```

要求数据哈希、分区、case、协议、攻击版本和适配器集合一致。外部模型身份与执行模式也必须一致；mock 不能与 live 混算。检测类别精确率/召回率下降、攻击成功率上升、任务完成率下降会触发退化。缺失/失败导致配对不完整时拒绝比较。它是确定性回归规则，不是置信区间或显著性检验；真实模型评测还需要多次独立运行与预先声明统计方案。

## Pi Agent 参考骨架

参考用户提供的 `pi-main/packages/evals` 的 harness/评分分离思路，以及 `pi-main/packages/agent` 0.84.4 的 Agent API。所有新增示例位于 `evals/pi`，没有修改或执行 pi-main 的全量测试、安装脚本和模型调用。

先导出可见输入，再运行完全离线的协议 fake，最后重新评分：

```powershell
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split all --export-attack-requests .review/ef1-requests.json
node evals/pi/run-mock.mjs .review/ef1-requests.json .review/ef1-responses.json
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split all --pi-responses .review/ef1-responses.json --output .review/ef1-pi-report.json
node --test evals/pi/attack-harness.test.mjs
```

fake 只返回合法空数组，是协议测试，报告中固定 `mode=mock`。它不是 pi-agent-core 实际推理，也不能证明真实模型攻击失败。新 Windows 运行环境执行这些 Node 示例需要已有兼容 Node（本机 Node 24 验证），Python 主评测无需 Node。

可选 `pi-example.mjs` 使用真实 `Agent` 构造器。需要调用方显式准备 0.84.4 包环境、选择 model 并注入 streamFn，再调用 `evaluateWithPi(batch, {model, streamFn, allowModelInvocation:true})`。函数没有自动模型/凭据发现、默认 provider、工具、技能或 cwd 文档加载。模型接收视图 JSON，输出严格的身份/属性元组；每条请求创建新 Agent，防止案例串扰，默认一轮、30 秒协作式超时。

真实 provider 配置不属于本次默认运行，示例没有自动安装依赖或付费调用。`tools=[]` 和阻止工具回调不是 OS 沙箱；streamFn 是受信任代码，可能有自己的网络行为。超时会 abort 并停止批次，不承诺强制结束任意不合作 provider。真实模型多轮搜索、浏览器攻击、算力/费用预算及统计重复是后续扩展。

外部响应必须带 request_id、请求摘要、status、identities、attributes，外层记录 implementation/version/provider/model/mode。摘要绑定的是可见请求，不是黄金答案，也不是发行者签名或模型执行证明。未知请求、重复 ID、过期摘要、无效 JSON、超时和缺失不能作为有效弃答。原始猜测不进入最终统计报告。

## 测试与交付范围

`tests/test_effectiveness_eval.py` 覆盖位置/类别评分、空分母、错误猜测、歧义与重复背景、否定/引用、历史桥接、配对拒绝、冻结数据、真实候选、错误消息边界、Pi 协议往返、失败与缺失、CLI 门禁。`evals/pi/attack-harness.test.mjs` 使用 API fake 测试视图边界、输出校验、失败、超时和批次输入校验。

历史阶段验证使用 Windows 本地环境；当前结果应以实际测试和评测输出为准。CI配置增加了 Python 评测和 Node 协议测试，但未声称远程 CI 已执行。D5 试用包和真人试点状态保持原样。

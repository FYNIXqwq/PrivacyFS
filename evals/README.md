**D1 冻结评测集**

`data/d1-v2.jsonl` 全部为合成数据：1,008条实体定位样本、60组文件场景、20个结构保留任务，共1,088个case。manifest记录版本和SHA-256，runner拒绝哈希不匹配的数据。

实体样本包含21类变体，每类48个样本；共享record/CSV上下文的编码、简繁及字符变体合并为同一模板族，只能属于一个分区。development为712个case，validation为376个。runner还检查规范化后相同的输入不跨分区。模板数量和独立性有限，不能把1,008个变体当作1,008个独立现实任务。

早期d1-v1及其哈希保留供审计，不用于当前验收：初版按字符变体命名分组，会把相同上下文的简繁变体分到两边。v2修正分组，未根据评测得分更改目标标签。当前严格runner会拒绝v1的跨分区重复。

评测给定明确名单或正则，测试编码、规范化、来源位置、CSV结构与状态。它不衡量未知姓名NER准确率，不评分跨文件实体推理，也不评分LLM摘要质量。file_group中的未来D2标签只描述场景，不计作D1已实现能力。task_preservation只验证原始内容/表格结构保留。

运行（从仓库根目录）：

```powershell
.\.venv\Scripts\python.exe evals/run_d1.py --split development
.\.venv\Scripts\python.exe evals/run_d1.py --split validation
.\.venv\Scripts\python.exe evals/run_d1.py --split all
```

报告包括精确类别/字符位置的TP、FP、FN，解析状态正确数、来源定位检查数和结构保留结果。报告中的数据哈希对应公开合成语料，不来自用户真实文件。真实文件检查报告不会输出内容哈希。

`build_d1_dataset.py` 是数据来源，不在测试中自动重建冻结文件。调整标注或模板时审查差异，使用新的版本名，例如 `--version d1-v3`，不得修改旧文件后只刷新哈希以掩盖失败。保留集结果不能用作持续微调阈值的开发反馈。

CI的快速测试校验哈希、case数量和分组，并单独运行validation分区；完整语料由显式评测命令执行。无远程模型、外部上传或真实客户数据。运行时只在新建临时目录生成合成文件并自动清理。

**D2 冻结关系评测集**

`data/d2-v1.jsonl` 包含256个合成多文件场景变体，属于16个固定拓扑、类型/namespace、字段词汇和文本包装组合的模板族。development为176个case，validation为80个case（5个完整模板族）。每族16种扰动整体保留在同一分区，runner校验哈希、分组、相同文档不跨分区、出现位置和来源回放。部分扰动主要检查附加同名主体或重复副本，不能解释成256种独立算法挑战。

该集包含显式编号正例、错误编号、前导形式差异、不同namespace/type、否定、引用、未知敏感值、同名不同主体、复制文件以及准标识符冲突。更复杂的替代通路、双向矛盾断言、人工纠正与资源/生命周期由正式回归测试补充。它们不计为保留集分类精度分母。

`build_d2_dataset.py` 只负责生成最初数据，发现已有版本会拒绝覆盖。字段提取的黄金定位和编号主体标签存于冻结文件；评测不根据模型预测生成目标标签。数据来自同一合成生成器，不是盲测现实材料，也不支持未知NER/通用语义效果结论。未进行真实客户或模型摘要效用测试。

```powershell
.\.venv\Scripts\python.exe evals/run_d2.py --split development
.\.venv\Scripts\python.exe evals/run_d2.py --split validation
.\.venv\Scripts\python.exe evals/run_d2.py --split all
```

指标分别报告：显式编号出现位置的实体连接对TP/FP/FN；每类规则按case是否触发的TP/FP/FN/TN；每case精确风险数量不符记录；跨文件增量和所有witness来源回放。连接对共享主体、同族变体相关，不能把分母当作独立统计样本。误合并、漏连接、数量差异、定位错误和基线偏差以安全case ID写入errors；空errors只说明该冻结范围内未观察到错误。门槛保留precision≥95%、recall≥90%。

逐文件基线使用同一规则引擎，限制可见证据来源，并保持实体修订固定；它是可见文件范围的消融对照，不是独立检测器。D2实际验收在无人工纠正的revision 0进行。当前数值以评测输出为准。

**D3 冻结任务效用集**

`data/d3-v1.jsonl`包含60个结构化任务样本，统计、记录摘要准备、排障材料各20个；10个模板族整体划分，development 42、validation 18。变体覆盖CSV/TXT/Markdown、引用/否定状态、多行和引号、未配置私有列、明确泛化。它们来自同一合成生成器，不是60个独立真实用户工作流。

生成器`build_d3_dataset.py`拒绝覆盖已有版本。`run_d3.py`校验hash、模板族分区与重复文档，然后构建计划、将候选实际写入合成临时目录、重新读取验证。黄金期望独立记录事实序列及断言状态；runner检查事实计数、键关系、必须消失的原值、原件字节和全抑制对照。

```powershell
.\.venv\Scripts\python.exe evals/run_d3.py --split development
.\.venv\Scripts\python.exe evals/run_d3.py --split validation
.\.venv\Scripts\python.exe evals/run_d3.py --split all
```

此评测验证候选字节的结构化效用，不执行自然语言摘要或自动诊断，也不衡量用户审核时间。完整批准/发布/恢复协议由`test_release_lifecycle.py`、真实CLI测试和三个实际发布示例分别验证；不能把SDK候选评测说成60次完整对外发送。没有外部数据或模型调用。

有限格式下预期事实、关系和原件字节必须全部一致；每个案例需有可行方案比全记录抑制保留更多必要信息。错误报告使用合成case ID，不写真实路径或实体原文。当前结果应结合案例覆盖范围与实际输出判断。

**D4 持久缓存与增量回归**

`run_d4.py`复用d2-v1的冻结分区，默认80个validation场景。每个场景进行cold、warm、桥接编号changed、forced full四轮；核对原黄金规则、冷/热结果、增量/全量结果，以及修改桥接后的断连。warm必须没有解析或图重建。

```powershell
.\.venv\Scripts\python.exe evals/run_d4.py --split validation
```

这是既有合成数据上的实现回归，不是新的独立识别精度评测，也不据此修改d2-v1标注。正式测试另覆盖持久纠正、取消、源变更、迁移、损坏、清理和发布。基准`benchmarks/workspace_d4.py`测量同一已校验SDK存储中的冷/热/增量阶段，排除CLI/权限初始化和真实人审时间。

历史阶段报告仅本地保存，当前测试数量与耗时以实际运行结果为准。

**D5 有限文档投影回归**

`run_d5.py`复用冻结d3-v1文本及黄金任务结果，将CSV包装为DOCX简单表格、字面记录包装为文本型PDF页。每个复杂输入保持PARTIAL；使用显式source_view后验证抽取位置、事实、关系、原值/未处理内容残留，以及源字节不变。

```powershell
.\.venv\Scripts\python.exe evals/run_d5.py --split validation
.\.venv\Scripts\python.exe evals/run_d5.py --split all
```

全量60任务、180复杂输入、720次来源回放及180个新CSV产物均通过，保留120个预期事实行。转换器版本单独报告，不修改原d3-v1文件或哈希。这是合成格式/任务回归，不是现实PDF/DOCX布局、OCR或字体准确率盲测。

正式测试另覆盖拒绝路径、实际子进程内存/超时、诊断和安装保护。新venv离线安装、槽位切换与卸载记录由单独交付检查生成；真实用户反馈仍未开展。历史交付记录仅本地保存。

**EF1 效果与攻击评测**

新增 `run_effectiveness.py` 和 `effectiveness/`：严格位置/类别检测、四种处理对照、独立确定性攻击者、任务行与归属关系效用、配对差值和基线退化门禁。`data/effectiveness-v1.jsonl` 是22个合成挑战场景，包含未知实体与误报负例，不能因为当前识别不到就删除标签或降低目标。原D1～D5冻结文件保持不变。

```powershell
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split all --include-cross-file --output .review/ef1-report.json
node --test evals/pi/attack-harness.test.mjs
```

输出文件只新增、不覆盖。默认低得分和残余攻击是观测；执行失败/缺失响应属于错误。无真实模型、客户数据或网络调用。`pi/`参考用户提供的Pi Agent API，fake只验证协议，真实适配器需要显式模型配置。指标定义、数据族限制、Pi往返、基线比较和退出码见根目录 `EVALUATION_FRAMEWORK.md`；当前结果以评测输出为准。

# D3：任务约束、审核与本地验证发布

D5补充：0.2.0a1支持DOCX/PDF明确文本选区，经相同任务/产物验证后只生成CSV。原文件保持PARTIAL和未处理项记录，见FORMATS_D5.md；没有原格式完整匿名化能力。

当前补充：D4已经提供持久工作区纠正、本地终端原文对照及Windows DPAPI状态保护，见`WORKSPACES_D4.md`。普通prepare/review流程仍可独立使用；工作区纠正需通过workspace prepare进入计划。

D3在D2显式关系模板上生成有限格式的副本：保留任务允许的事实、使用本批一致的实体化名、丢弃未配置内容，再检查实际产物。支持CSV和D2的一行一记录TXT/Markdown；不支持任意自然语言文档的自动完整脱敏。`privacy_verified`始终为false，`VALIDATED/PUBLISHED`表示声明的任务及已知值检查通过，不是匿名化或法律合规认证。

## 快速运行合成项目

从仓库根目录执行。下面只处理公开合成样例；状态目录必须尚不存在，或已经由PrivacyFS创建。

```powershell
.\.venv\Scripts\privacyfs.exe prepare examples/d3/statistics `
  --relations examples/d3/statistics/relations.json `
  --task-profile examples/d3/statistics/profile.json `
  --state-dir .review/my-d3-state
```

返回`plan_id`、`approval_digest`、候选方案、选择结果、字段处理方式与损失计数。先检查该具体计划：

```text
privacyfs review PLAN_ID --state-dir .review/my-d3-state
privacyfs review PLAN_ID --state-dir .review/my-d3-state --approve APPROVAL_DIGEST
privacyfs export PLAN_ID .review/my-d3-output --state-dir .review/my-d3-state
privacyfs release show RELEASE_ID --state-dir .review/my-d3-state
```

`PLAN_ID/APPROVAL_DIGEST/RELEASE_ID`使用实际JSON返回值，不是固定示例字符串。`review --approve`是产品内对该摘要的批准；仅查看计划不会批准。导出前必须有有效批准，不会因为输入中出现“允许上传”或任务标签而放宽策略。

最终目录为`DEST_CONTAINER/RELEASE_<本次随机scope>`，其下是中性`DOC_<token>.csv/.txt/.md`及`.privacyfs-release.json`。源目录名称、原文件名、路径结构和配置文件不会被复制。不同发布创建新目录，已有目录不覆盖。`release show`接受plan_id或release_id，并重新检查已发布文件的字节完整性。

另外两个可运行项目分别为`examples/d3/summary`和`examples/d3/diagnostics`，使用各自的`relations.json/profile.json`。它们分别保留结构化记录事实、错误码与重试次数；没有运行LLM摘要或诊断模型。

## TaskProfile契约

关系配置仍使用D2的`d2-templates-1`；任务配置使用独立的`d3-task-profile-1`。它们都属于可信本地配置，不由正文中的指令修改。

```json
{
  "schema_version": "d3-task-profile-1",
  "task": "collaboration_statistics",
  "recipient": "local-statistics-reviewer",
  "preserve_rows": true,
  "preserve_relations": true,
  "forbidden_terms": [],
  "fields": [
    {"name": "客户编号", "output_name": "customer_id", "mode": "alias"},
    {"name": "合作状态", "output_name": "status", "approved_values": ["暂停洽谈", "终止合作"]}
  ]
}
```

任务种类只有三个：`collaboration_statistics`、`record_summary`、`incident_diagnostics`。标签用于说明用途，**实际权限来自字段约束**；同一个任务标签不会自动允许所有商业信息或所有日志。接收方标识必填、存于私有配置和批准绑定中；不会作为上传地址使用，也不发起网络传输。

每个字段包含原字段`name`、公开字段`output_name`、`mode`及可选约束：

| 设置 | 作用 |
|---|---|
| keep | 保留逻辑值；必须有明确的approved_values，或采用关系模板中的有限values词表 |
| alias | 仅允许key/identity；按解析后的实体分配本批代号，不保留真实编号/名称 |
| generalize | 按显式generalizations对象把原值映射到批准的桶值；未定义输入拒绝 |
| drop | 整列/字段不带出；required=true时禁止 |
| required | 默认true；必需字段及其事实行不能被候选方案移除 |
| approved_values | keep的字面值白名单；不执行表达式、正则或模型判断 |
| generalizations | 例如`{"深圳":"华南","广州":"华南"}`；定义了可接受的精度损失 |

原字段不存在、输出字段名重复、类型错误、未知配置、必需字段要求删除等均拒绝。公开字段名限定为小写ASCII字母开头、字母/数字/下划线，不超过40字符；`assertion_state`为保留字段。

未出现在TaskProfile中的key默认保留为本批化名，其他字段默认删除。未配置的CSV列也全部删除，不依赖它是否恰好没有命中检测器。主键不得删除；`preserve_relations=true`时其他引用键也不得删除。身份和编号不能keep或generalize成原值，仅alias或drop。

保留事实必须在明确值白名单中。D3不把任意自由文本误称为“已处理安全区域”；未知值、未定义泛化、unknown断言直接拒绝。positive、negative、quoted的断言状态被保存在生成的`assertion_state`中，不能把否定或引用改成肯定事实。

## 候选方案和损失

引擎生成最多三个方案：

1. `requested`：执行已配置的字段保留、化名、泛化和删除。
2. `essential_only`：额外舍弃非必需的事实字段，保留必需项与已有保留键。
3. `suppress_all`：抑制全部记录，作为保守对照；违反任务行/事实要求时不可选择。

每个方案都接受真实字节和任务检查，再从可行方案中选损失最小者。损失单位是透明的规则计数：keep=0、alias/generalize=1、drop=2、suppress=3，另计未配置列的舍弃。它不是信息熵、泄露概率、业务价值评分或全局最优证明。

动作关联文档序号、行、字段、SourceLocator和已有D2证据ID。没有关系证据的未配置列，删除动作仍保留原文位置；它不会伪造证据边。同一逻辑字段只采用一种动作，CSV不对序列化引号内部做零散字符串替换。

公开review返回候选的中性文件名、字段输出名、处理方式、行数和动作计数。原路径、原值、对照表和接收方不进入该JSON。本地SDK可查看plan.records、candidate.actions和artifact.payload；这些内部对象含敏感内容，只能用于受控本地审核。普通D3命令没有GUI或原文对照界面，D4的workspace review --local提供本地对照。仍没有手动任意编辑候选字节的入口；需要调整时修改可信profile并重新prepare。

## 同批关系与跨批隔离

每个计划生成新的32字节随机seed，按图中实体的确定性成员描述产生本批化名。相同实体在多份文件中使用相同代号；同名不同编号不会因为显示名相同被合并。scope、文件名和化名与旧mapping.db无关；不会重编号旧PERSON/ORG代号，也不会写入旧映射表。

同一计划重建时沿用seed、核验相同输入；新prepare使用新seed，即使源内容相同也默认隔离化名。隔离的是标识符作用域，不意味着事实值、行数和关系不能支持跨批关联。

活跃SDK计划绑定D2分析版本，确认/拆分/拒绝使旧计划失效。普通prepare每次从显式配置重建revision 0，不自动采用其他工作区的纠正；持久纠正和增量通过D4的workspace入口使用。不要把尚未保存的SDK内存修改当成CLI已经保存的修订。

## 产物验证范围

验证包括以下独立于“删掉风险对象”的条件：

- 用标准CSV reader检查实际CSV字节；TXT/Markdown逐行检查生成语法，然后用D1重新解析实际字节。
- 按原快照逐行验证必需字段、原值或批准的泛化值、实体化名关系及断言状态；禁止行重排或值被悄悄修改。
- 对原identity值和显式forbidden_terms做规范化后的子串残留检查，覆盖全角、大小写、局部简繁转换。短名字可能因此保守拒绝。
- 对ASCII编号按字母/数字/下划线边界检查；避免把随机化名内部的一个数字误认为原编号。CJK编号按子串检查。这不覆盖任意未知编码或语义改写。
- 检查输出字段名和文件名，并从重新解析的产物重建D2图；残留身份映射、编号风险或低支持度准标识符组合拒绝。保留的quasi字段检查2～8项的全部子组合，超过支持范围拒绝，不以有缺项的完整并集代表所有组合。
- 生成新UTF-8文件，重建CSV引号/换行或字面记录；不复制原编码BOM、原时间属性、原附件、配置、未解析正文或原始元数据。
- 暂存目录必须只有计划文件和固定marker；逐字节摘要与已验证候选一致，普通文件无硬链接、重解析点和额外条目。
- Windows检查文件及目录的NTFS命名数据流；发现额外流拒绝。非Windows代码拒绝已枚举扩展属性，但当前验收环境为Windows。

以`= + - @`、tab或回车开头的保留值会拒绝，防止CSV被表格软件当作活动内容；负数等合法业务值也可能被保守拒绝，首版不自动改写数值来绕过。这是限定格式的防护，不是对任意表格软件的完整执行隔离。

已通过内容检查的字节才会写入私有暂存区；写入后再检查整棵目录与字节摘要、重新读取源和配置，最后才切换到可见发布目录。摘要一致证明磁盘文件与此前解析的候选字节一致，不以“没有抛异常”代替验证。

## 状态、批准、失败与恢复

成功准备后以原子记录保存为VALIDATED。构造/规划阶段在内存执行，不持久化半成品DRAFT/PLANNED记录；没有可行方案时直接返回拒绝。主要持久状态：

```text
VALIDATED → APPROVED → STAGING → PUBLISHED
                 ↘ FAILED
输入/策略变化 → NEEDS_REVIEW
中断恢复：STAGING → RECONCILING → APPROVED 或 PUBLISHED
```

批准摘要绑定源文件的私有HMAC、关系/任务配置字节、接收方（通过profile）、处理版本、图修订、动作位置与类型、scope及候选文件摘要。重新批准与导出均重新构建并校验；输入、规则、接收方或产物变化使旧批准失效。NEEDS_REVIEW须重新prepare；普通写入失败的FAILED计划可在输入未变时再次明确批准。

状态存储采用单写者OS锁、带HMAC的记录、临时文件替换和fsync。D4起，Windows新密钥和计划私有部分使用用户DPAPI保护；旧D3明文记录可读取并逐条迁移。它不防同账户进程或管理员，不是不可篡改审计链。非Windows的acl-only通道不是加密。原映射数据库保留，不做SQLite迁移。

暂存区固定在`STATE/staging/PLAN_ID`；状态目录与发布容器必须位于同一文件系统。最终发布始终创建新`RELEASE_*`目录，不替换旧发布；目标已存在就拒绝。容器必须与源目录、状态目录相互分离，不能互为祖先或后代。只表示本地文件发布成功，没有外部发送或披露账本。

中断后运行：

```text
privacyfs release recover PLAN_ID --state-dir PRIVATE_STATE
```

若目标目录已完整匹配批准记录，则确认PUBLISHED；若尚未切换，则清理已知暂存文件并回到APPROVED，下一次导出仍重新核验输入。出现未知暂存内容、外来目标或含糊状态时保留现场并拒绝，不删除或覆盖它们。真正进程退出及切换前后中断均有回归测试。

Windows源需保持静止；最后一次源核验与目录rename间仍存在很小的竞争窗口，不保证抵御同账户恶意写入。实现不提供一般性文件系统/状态数据库原子事务、断电证明、内核沙箱或进程网络限制。

## 私有状态与旧命令兼容

状态目录在创建时设置仅当前账户允许的ACL；已有状态目录必须保持受保护ACL和匹配所有者。Windows通过系统.NET目录安全接口操作新建目录的DACL，不要求设置新所有者。无法建立/验证私有权限时拒绝继续；不会把普通已有目录接管为状态目录。POSIX使用0700和所有者检查，未在本阶段做该平台完整验收。

`.privacyfs-state.json`是新的保留排除标记。旧scan/analyze/mirror/tui遍历、隐藏目录大小统计和D1/D2正文入口都会跳过或拒绝该目录及其后代；即使标记损坏也不读取密钥后再决定。这是有意的兼容变化：普通目录中放置同名标记也会触发排除。它不授权删除任何内容。

状态包含源路径、seed、配置绑定与发布历史，按敏感资产管理，不能共享或作为Agent工作目录。D4已增加加密、保留/清理和持久纠正；ACL/DPAPI仍不隔离同账户Agent，也不能保证不受控备份或交换文件安全。运行时边界仍属于D6。

## 限额、退出码与现有范围

复用D1/D2默认文件、字符、断言、图深度和阶段时间限额；D3最多100,000个动作、64 MiB候选字节、1,000个计划、单条状态记录4 MiB。持久流程没有放大D1默认16,000,000保留字符预算，因此不承诺默认CLI处理任意50 MiB项目。权限辅助进程最多等待30秒；单写者锁不无限等待。

退出码：0是所请求操作完成；2是参数/配置错误；3是策略、批准、完整性或边界拒绝，也包括无法构造有效输入快照的INPUT_CAPTURE_FAILED；4是其他OS读写、解析或资源处理失败。早期参数/OS错误可能只有安全stderr；有JSON时使用d3-release-status-1。失败不会返回成功发布状态，也不会自动保留未校验输出为可见产物。

支持的是显式结构化字段准备：统计值/分组关系、事实材料、错误码等。任意日志文本、复杂Markdown、DOCX/PDF、OCR、自由文本匿名化、高保真原格式编辑、跨发布累计披露都未包含。可通过本文列出的测试和基准命令在目标环境复验。

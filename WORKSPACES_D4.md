# D4：本地审核、持久纠正、增量与状态治理

D5补充：0.2.0a1可将DOCX/PDF的明确文本选区接入本工作流，见FORMATS_D5.md。原文件仍为PARTIAL，复杂格式产物只生成CSV；不能把缓存/工作区ready理解为整个Office/PDF已安全。

D4在D1～D3的显式TXT/Markdown/CSV工作流上增加可持续使用的工作区。它保存加密索引、解析/关系缓存和纠正记录，提供本地终端中的来源与处理前后对照，并使纠正进入D3发布流程。仍不理解任意自然语言、不提供GUI或操作系统Agent沙箱，`privacy_verified`始终为false。

## 建立工作区

从仓库根目录运行合成例子：

```powershell
.\.venv\Scripts\privacyfs.exe workspace init examples/d3/statistics `
  --relations examples/d3/statistics/relations.json `
  --task-profile examples/d3/statistics/profile.json `
  --state-dir .review/my-d4-state
```

返回`workspace_id`和当前`revision`。重复使用相同根及配置路径会找到现有工作区；不同工作区即使内容相同，也不共享实体索引或可关联的公开ID。源、状态和输出的原有路径隔离规则继续生效。

```text
privacyfs workspace list --state-dir PRIVATE_STATE
privacyfs workspace status WORKSPACE_ID --state-dir PRIVATE_STATE
privacyfs workspace refresh WORKSPACE_ID --state-dir PRIVATE_STATE
privacyfs workspace refresh WORKSPACE_ID --full --state-dir PRIVATE_STATE
privacyfs workspace review WORKSPACE_ID --state-dir PRIVATE_STATE
```

`refresh/review`核对当前源文件，成功报告`source_checked=true`。`status/list`只读取最后提交的索引状态，`source_checked=false`，不把旧结果当作已核验当前文件。读取/解析失败时风险数量不会伪装成零；用status取得当前修订号，修复输入或配置后再刷新。

文件仍由关系配置显式绑定。未绑定的新文件不自动进入正文分析；删除或重命名绑定文件时，也应更新配置。找不到仍被绑定的文件会报错，而不是悄悄跳过。

## 本地私有审核

在真实交互终端中执行：

```text
privacyfs workspace review WORKSPACE_ID --local --state-dir PRIVATE_STATE
```

无头调用或输入/输出重定向时，`--local`被拒绝；默认review只返回安全JSON。原文接口没有HTTP服务或远程传输功能。本地SDK仍是有原文访问权的私有接口，不能把它作为普通远程Agent报告。

终端命令：

| 命令 | 操作 |
|---|---|
| refresh | 核验源并更新当前修订 |
| sources | 显示当前源文件路径及安全ID |
| occurrences | 查看原编号、类型/namespace、出现位置及实体ID，供拆分/确认 |
| evidence EVIDENCE_ID | 展示这组事实的全部来源路径、行/列/字符位置及原文覆盖 |
| confirm ENTITY_ID… | 明确确认单实体或合并多个同类型、同namespace实体 |
| split ENTITY_ID OCCURRENCE_ID… | 把选定出现位置从该实体拆开 |
| reject_evidence EVIDENCE_ID | 排除所选建模事实及本次有效来源副本 |
| reject_candidate CANDIDATE_ID | 拒绝同名共指候选，保留双方实体事实 |
| reject_entity ENTITY_ID | 从活动证据中排除该建模主体 |
| undo | 撤销最近一项仍有效的操作，包括已过期纠正 |
| profile PATH / relations PATH | 切换到经过合法性检查的本地配置文件；不改写原配置 |
| plan | 生成D3计划并显示原文/候选的before/after对照 |
| approve PLAN_ID DIGEST | 对已查看的具体D3计划明确批准 |
| quit | 退出审核 |

对照默认预览每个文件前40行、每行512字符，并明确标出截断。完整数据可通过私有SDK读取；没有把截断预览当作完整解析覆盖。JSON引用及方向控制符转义避免原文被直接解释成终端控制命令。文本中的操作指令只是数据，不会自动触发纠正或批准。

命令行形式也可使用安全ID执行持久操作：

```text
privacyfs workspace correct WORKSPACE_ID reject_evidence EVIDENCE_ID --expected-revision N --state-dir PRIVATE_STATE
privacyfs workspace correct WORKSPACE_ID split ENTITY_ID OCCURRENCE_ID --expected-revision N --state-dir PRIVATE_STATE
privacyfs workspace undo WORKSPACE_ID --expected-revision N --state-dir PRIVATE_STATE
privacyfs workspace configure WORKSPACE_ID profile PROFILE_FILE --expected-revision N --state-dir PRIVATE_STATE
```

所有写操作使用`expected_revision`防止在过期界面上应用修改。控制台自动使用最近一次显示的修订；源已变时要求刷新。撤销采用后进先出，不提供任意删除历史中间步骤或自由组合重做；这样不会在依赖关系已改变时默默执行后续修订。

## 纠正范围与发布

纠正保存工作区、动作、目标安全ID和具体来源版本/模板指纹。确认、拆分、拒绝会增加工作区修订并使相关未发布计划失效；同名候选拒绝不会把真实事实删掉。

来源内容、编码或解析/规范化/规则版本变化后，受影响纠正标为stale，不自动套到新文字位置或新路径。工作区报告会显示needs_review；有活动stale纠正时不能准备发布计划。可撤销该纠正、查看新来源，再作新的明确修订。内容恢复为旧字节不会自动重新激活已经过期的纠正。

使用持久工作区准备计划：

```text
privacyfs workspace prepare WORKSPACE_ID --state-dir PRIVATE_STATE
privacyfs review PLAN_ID --state-dir PRIVATE_STATE
privacyfs review PLAN_ID --approve APPROVAL_DIGEST --state-dir PRIVATE_STATE
privacyfs export PLAN_ID OUTPUT_CONTAINER --state-dir PRIVATE_STATE
privacyfs release show RELEASE_ID --state-dir PRIVATE_STATE
```

新的`workspace prepare`会重放有效纠正，并将工作区修订加入批准绑定。原`prepare ROOT ...`仍保留D3独立流程，不能把它误认为使用了某个工作区的纠正。当前已发布记录保持历史含义；修改纠正或删除源不会撤回之前发布的文件。

输入或纠正修订变化后，旧批准即使因中断尚未在磁盘显示NEEDS_REVIEW，读取/导出时仍会按当前工作区修订拒绝。原文保护和产物残留验证独立于纠正；“拒绝某条风险证据”不是允许原姓名被带出的授权。

## 增量如何工作

每次刷新仍读取所有选定文件的字节，核对完整内容，而不是只信mtime/size。这些读取具有D1的前后校验和不跟随重解析点规则；它仍不是多文件原子快照，源文件应保持静止。

缓存分层：

1. **内容解析缓存**：工作区内按内容、格式、显式编码、限制和解析/规范化版本索引。保留IR、逻辑值与原文覆盖映射。相同字节副本可复用解析。
2. **来源实例**：路径对应独立文档ID，内容版本及定位绑定到该实例。副本不是同一文件；重命名不会自动继承旧定位或纠正。
3. **关系与风险缓存**：按依赖分组缓存完整实体图和已计算的风险。未受影响的分组不重新抽取实体或重新搜索风险通路。
4. **计划依赖**：D3计划绑定工作区修订、输入、profile/接收方和产物摘要。最终发布仍执行D3真实产物验证，不以缓存命中替代发布门禁。

依赖分组采用保守连接：相同类型/namespace/原编号、同类名称候选、准标识符支持度群组，以及人工纠正的来源范围都建立依赖。准标识符数量会受其他主体影响，因此不能只按共享编号划分。来源变化导致分组合并或拆开时，允许重算整个受影响分组。

缓存键区分数据、模板、编码、解析/规范化、关系规则、攻击假设和相关纠正。profile变化可以复用正文图，但仍增加工作区修订并使计划失效。冷启动、无变化重跑、增量及`--full`在相同有效输入和纠正下应有相同语义结果；公开ID在工作区内稳定，跨工作区隔离。

可见指标包括`parsed_documents/parse_cache_hits`、`rebuilt_components/graph_cache_hits`及耗时。新增无关文件通常只解析和重建它自己的分组，但仍有全文件字节核验、索引扫描、缓存解密和合并结果的成本。不能声称所有工作都变成常数时间或任意小改动都仅影响一个节点。

当配置中有一组唯一的删除/新增文件拥有相同字节时，事件可标`possible_rename_to`；它只是提示，不是文件身份迁移。多副本歧义不作唯一配对。按原显式编号得出的实体关系与按路径猜测身份是不同机制。

## 加密、迁移与权限

Windows默认使用当前用户DPAPI保护：新工作区索引、正文/图缓存、D3计划的私有部分和HMAC密钥均不以明文落盘。计划文件仅留下plan_id和状态等少量头部。HMAC绑定密文和保护提供者，检查误改、损坏和跨作用域错配。没有自制加密算法。

旧D3明文key和计划仍可读取。`workspace init`逐条原子升级它们：先校验解包结果，保留相同密钥、plan_id、scope、批准绑定及发布历史，再替换对应记录。中途停止可再次初始化继续迁移，不要求重新分配旧化名；旧mapping.db完全不参与迁移。

新版本兼容读取旧格式，不代表旧D3程序能读取D4格式。没有自动降级命令；需要降级时应使用升级前的受保护备份，并独立保留新版本已经产生的发布历史。恢复旧本地状态不会撤回外部已获得的副本。

DPAPI通常绑定当前Windows登录身份和机器环境，存在域配置等例外；同账户进程仍可以解密。它不是Agent沙箱，不防同账户恶意代码或管理员接管。恢复依赖有效的Windows/DPAPI环境，不能保证仅把密文目录复制到另一台电脑就可恢复。应按系统能力备份相应用户恢复材料，并保护备份副本。

密钥保管接口在`state_keys.py`。非Windows暂有显式`acl-only`提供者，报告会显示storage_protection；该模式不是加密，本阶段没有完成非Windows平台验收。外部硬件密钥、多用户和跨机器共享不在当前交付范围。

原有仅当前账户ACL、状态标记排除、禁止硬链接/重解析点、源/状态/目标互不包含继续生效。私有SDK原文不得进入普通JSON或远程日志。`.privacyfs-state.json`边界覆盖所有新增索引及缓存，旧镜像管线不复制它们。

## 保留、清理与恢复

默认只保留当前索引引用的缓存；成功提交新索引后清除不再引用的旧内容/旧图。正在使用的相同字节副本仍可共享内容缓存；删除绑定后只有在没有其他引用时才清除对应内容缓存。

```text
privacyfs workspace prune WORKSPACE_ID --state-dir PRIVATE_STATE
privacyfs workspace prune WORKSPACE_ID --apply --expected-revision N --state-dir PRIVATE_STATE
privacyfs workspace prune WORKSPACE_ID --retention all --expected-revision N --state-dir PRIVATE_STATE
privacyfs workspace prune WORKSPACE_ID --retention current --expected-revision N --state-dir PRIVATE_STATE
privacyfs workspace forget WORKSPACE_ID --expected-revision N --state-dir PRIVATE_STATE
```

prune默认只报告可清理的缓存文件数/字节；`--apply`才删除。retention=all保留旧缓存供本地复现，但仍受配额限制；current恢复默认后，后续成功刷新会清除旧缓存，也可显式apply立即清理。

forget删除该工作区的索引、纠正和缓存，使其未发布计划失效。已发布文件及其发布历史保留，不表示撤回披露。它不删除源文件、旧mapping.db或整个状态根；若需要处置已发布历史，须另作明确的数据保留决定。

写入使用同一状态根的单写者OS锁；缓存先写，索引最后原子提交。中断后多余缓存不会被当作当前结果，后续prune可清理。清理先记录purging，再按已验证的本工作区路径逐个删除；未知文件、目录或重解析点会保留并拒绝，不递归删除外来内容。中断初始化的空壳会在list显示，可用forget清理。

公共review/refresh/correct/prepare服务自行持锁。使用WorkspaceView或下层写存储API时，调用方必须处于releases.lock上下文；不支持并发修改同一个视图实例。一次文件原子替换不等于整个工作区与所有计划文件构成单一原子事务。

缓存损坏不会退化为“无风险”。`refresh --full`可用重新读取的源数据重建并覆盖损坏的派生缓存；索引、原文件或控制文件本身损坏时需先修复相应问题。丢失密钥不能靠忽略完整性检查恢复。

SDK支持Event取消，终端支持中断。取消的刷新不提交半份索引；导出取消不会发布不完整目录，并记录CANCELLED。D3的切换日志、真实进程中断恢复和新目录发布方式继续生效，不声称跨数据库/文件系统的掉电原子事务。

## 限额与验证边界

沿用D1/D2/D3文件数、字符、图规模、动作与产物限制。工作区刷新还有60秒整阶段预算；每个内容/图阶段仍使用已有预算。每状态根最多100工作区，每工作区最多1000项纠正、3000个缓存目录条目、128 MiB缓存，单个解包记录最多64 MiB。D3计划解包仍最多4 MiB。

source_checked只表示本次选定文件完成读取核验。ready表示索引/图处理完成，不代表TaskProfile可发布或数据匿名。未知、引用、否定、冲突另列入review_items/统计；stale纠正产生needs_review。失败时用status查看最后提交的修订，不能把旧报告当作当前成功结果。

本阶段测试使用合成数据和既有冻结回归集，未上传用户材料或调用模型。离线测试检查Python核心不建立网络socket；它不约束操作系统服务或用户显式选用的网络文件系统。实际计时和验收结果应在目标环境复验。

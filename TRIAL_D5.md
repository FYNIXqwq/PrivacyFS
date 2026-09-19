# PrivacyFS 0.2.0a1 本地试用指南

本包用于Windows本地试用：从DOCX选定表格、CSV索引、PDF选定页中保留批准字段，生成新的CSV副本。真人试用验证尚未开展；本包包含合成材料及记录模板，不含客户数据或模型权重。

## 安装

前置条件：Windows x64，已有Python 3.10、3.11或3.12 x64。安装器不会下载Python、HanLP或Ollama模型，不要求安装Word才能跑合成闭环。没有Python时先按组织允许的方式准备对应解释器；本阶段的干净验证是新venv，不是新装Windows裸机。

解压本包。在PowerShell中从解压目录执行，TARGET替换为一个新安装目录，其父目录应存在：

```text
py -3.12 manage.py install TARGET
```

也可以用已有解释器的完整路径运行manage.py，或用`--python-exe PATH`选择受支持的解释器。示例：

```powershell
python manage.py install C:\PrivacyFS-Trial
```

安装器校验包内SHA-256，再以`--no-index --require-hashes`安装本地wheel到新venv，运行pip check和DOCX/PDF自检。成功后使用`TARGET\privacyfs.cmd`。不修改全局PATH、系统Python、计划任务或服务。失败不会将未验证的runtime设为当前版本。

manifest和依赖哈希是损坏/内容一致性检查，不是代码签名或发行者认证；仅使用可信来源的包。DEPENDENCIES.json列出版本与wheel内许可文件，原许可文件随wheel保留。项目公开分发许可/签名不由这个本地包自动授予。

## 第一次合成试用

下面命令中的APP指TARGET\privacyfs.cmd，TRIAL建议为TARGET\data\sample。

```text
APP doctor --self-test
APP sample TRIAL
APP workspace init TRIAL\source --relations TRIAL\source\relations.json --task-profile TRIAL\source\profile.json --state-dir TARGET\data\state
```

sample只创建新目录，不覆盖已有材料。它包含DOCX客户表、CSV会议索引、PDF状态记录，以及只应被忽略的页眉、PDF作者和附件。预期任务结果：paused=1、ended=1。

使用实际返回的WORKSPACE_ID、PLAN_ID和APPROVAL_DIGEST：

```text
APP workspace review WORKSPACE_ID --local --state-dir TARGET\data\state
APP workspace prepare WORKSPACE_ID --state-dir TARGET\data\state
APP review PLAN_ID --state-dir TARGET\data\state
APP review PLAN_ID --approve APPROVAL_DIGEST --state-dir TARGET\data\state
APP export PLAN_ID TARGET\data\output --state-dir TARGET\data\state
APP release show RELEASE_ID --state-dir TARGET\data\state
```

--local需要交互终端，默认无头报告不含原文。在审核中用sources、occurrences、evidence ID、plan查看路径、来源位置和before/after。复杂格式的before是抽取文本，不是Word/PDF渲染页面；必须理解FORMATS_D5.md中的支持范围。

最终只出现新的RELEASE目录、CSV和发布marker。确认真实企业名、页眉、作者及附件不在输出中，status计数仍为1/1。不要把PARTIAL变成全文件安全结论；privacy_verified始终为false。

## 诊断

```text
APP doctor --self-test --bundle diagnostics.zip
```

诊断ZIP只含版本、能力、自检结果等安全字段；不收集源文件、路径、映射、模型地址、状态正文或原始异常。可选--state-dir仅报告状态可读性和计数，不迁移或打包其内容。诊断不自动发送，操作者检查后自行决定是否分享。日志和原文截图不应被随意附上。

## 更新、回滚、卸载

用新包运行同一install命令，会新建runtime槽位，自检通过才切换指针；旧runtime保留。中断指针切换会在下次操作时校验并恢复。没有通用自动后台更新。

```text
python manage.py rollback TARGET
python manage.py uninstall TARGET
```

回滚切换到前一个健康runtime，不回滚用户状态、不撤回发布。跨版本状态格式可能不兼容旧程序；升级前按状态文档保留受保护备份，并独立保存发布历史。

卸载只删除安装器记录的runtime和启动指针；TARGET\data及其他用户目录保留。发现状态/发布标记、数据库或重解析点混入runtime时会保留并拒绝清理。不要把工作文件放进runtimes；文件占用导致失败时先关闭程序，再重试。程序不自动删除旧mapping.db或发布历史。

## 试点边界

没有GUI、OCR、任意自然语言匿名化、原DOCX/PDF高保真编辑或Agent沙箱。只有明确选择并批准的结构化字段可进入CSV。技术自检和合成验收不能代替真实用户评价；使用PILOT_FEEDBACK_TEMPLATE.md记录实际安装成本、审核时间、任务正确性和重复使用情况。

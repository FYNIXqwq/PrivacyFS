# 配置覆盖范围
relations-commercial.json只绑定客户主数据、最新会议索引和最新状态摘录三个CSV。minimal模板删除主题，仅保留化名关联与状态；context模板保留完整主题，便于观察背景匹配风险。两者通过有限规则验证都不代表外部不可识别。

relations-employee-release.json只绑定花名册及8月薪酬。profile-employee-bands.json删除姓名/岗位/城市，按明确区间泛化税前应发，保留部门与期间。

relations-employee-audit.json额外纳入请假与期中绩效，包含unknown状态，只用于审计。relations-enterprise-audit.json还包含项目成员、项目台账和内部底价，共10个CSV绑定；图通路提示关联，不意味着事实属性可转移给所有可达员工。

rules.synthetic.yaml提供模拟名单用于inspect。它会暴露字面匹配边界，例如“白榆木色”与项目代号同形；名单命中不是语义结论。

未绑定的Markdown、邮件、日志、历史表仍是挑战材料，不会被relate/prepare偷偷读取。JSONL和EML没有当前正文解析适配；本例未制作或承诺DOCX/PDF/OCR支持。全场景与配置选区要分别计量。

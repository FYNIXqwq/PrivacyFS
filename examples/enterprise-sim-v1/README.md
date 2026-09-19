# 企业模拟用例：澄岚数智｜enterprise-sim-v1

一套可直接浏览和用于隐私评测的虚构企业共享盘。时间截面为2026-08-28，包含12名员工、4个外部客户、5个项目（其中1个内部项目）。人物、公司、价格、工资、病假、绩效、交易、邮件及网页背景全部由作者编造，不来自真实企业或个人；名称若有巧合不代表事实。邮箱使用.example，联系方式/账号/令牌均使用SIM前缀，日志IP使用文档示例网段，不含可用凭据。

## 目录边界

```text
workspace/          企业源资料：运营、人事、商务、交付、财务、研发运维、沟通及普通业务
attacker_context/   攻击者可选的虚构公开资料和历史披露
controls/           PrivacyFS显式关系配置、任务模板、模拟名单
evaluator/          评测员答案及EF1缩减切片；不得交给攻击代理
TASKS.md            不含答案的任务说明
manifest.json       文件清单、SHA-256与合成来源声明
```

模拟共享盘入口是workspace，不是本目录或整个PrivacyFS仓库。不要把evaluator、controls或评测答案一起上传给被测AI。这里的目录分离只是实验组织方式，不是文件系统权限沙箱；有任意文件访问权限的代理仍可能读取父目录。实际代理评测应只提供导出的可见请求或经隔离的视图。

## 场景特点

有项目代号与真实企业映射、工号与薪酬/请假、岗位与公开团队简介、主题与行业报道、旧披露与新状态等多步关联；也有两位同名员工、未经证实的客户传闻、否定引用、旧状态、待评定绩效和会议室/项目同名等负例。普通采购、培训与运维事务用于避免资料全部都是明显敏感记录。

这是一家公司的一次人工构造场景，不是现实企业盲测；不应按文件数把样本量夸大为独立企业数量。

## 从仓库根目录使用

```powershell
$pf = '.\.venv\Scripts\privacyfs.exe'
$case = '.\examples\enterprise-sim-v1'
& $pf relate "$case\workspace" --config "$case\controls\relations-commercial.json"
& $pf relate "$case\workspace" --config "$case\controls\relations-employee-audit.json" --quasi-field 部门 --quasi-field 工作城市 --quasi-field 岗位
& $pf prepare "$case\workspace" --relations "$case\controls\relations-commercial.json" --task-profile "$case\controls\profile-commercial-minimal.json" --state-dir '.\.review\enterprise-trial-state'
```

prepare只准备计划，不自动批准或发送。记录实际plan_id后使用review查看，确认才批准和export；源、state和output分别使用互不包含的目录。员工区间模板与带主题模板可用相应配置另建计划。完整产品步骤见仓库TRIAL_D5.md。

运行已有EF1对这个商业切片做实际候选与攻击对照（文件输出需新路径）：

```powershell
.\.venv\Scripts\python.exe evals/run_effectiveness.py --dataset examples/enterprise-sim-v1/evaluator/ef1-commercial-v1.jsonl --split all --output .review/enterprise-ef1-report.json
```

这个命令只评测一个结构化切片，不代表整套共享盘被识别。它使用EF1内置的保留主题处理策略，不等同于controls中的minimal模板。切片仅为development，不能用默认validation运行。

源数据完整性与配置可运行性验证结果由本次交付单独记录，不会自动调用真实Pi或模型。已有D1～D5、EF1冻结语料没有改写。

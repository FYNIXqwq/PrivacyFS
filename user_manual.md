# PrivacyFS 用户手册

> 适用版本：PrivacyFS 0.2.0a1（含 D0～D4 与 D5 技术试用交付）  
> 文档范围：安装、扫描、组合风险审计、策略执行、镜像、TUI、规则配置、映射管理、安全边界与故障排查。

---

## 目录

1. [PrivacyFS 是什么](#1-privacyfs-是什么)
2. [使用前必须理解的安全边界](#2-使用前必须理解的安全边界)
3. [安装](#3-安装)
4. [快速开始](#4-快速开始)
5. [核心概念](#5-核心概念)
6. [`scan`：生成脱敏清单](#6-scan生成脱敏清单)
7. [`analyze`：组合推断风险审计](#7-analyze组合推断风险审计)
8. [上下文策略模式](#8-上下文策略模式)
9. [`mirror`：生成脱敏镜像](#9-mirror生成脱敏镜像)
10. [`tui`：交互式浏览](#10-tui交互式浏览)
11. [`map`：管理化名映射](#11-map管理化名映射)
12. [规则文件](#12-规则文件)
13. [检测能力](#13-检测能力)
14. [组合风险与自动处理](#14-组合风险与自动处理)
15. [输出格式](#15-输出格式)
16. [本地 LLM 与 HanLP](#16-本地-llm-与-hanlp)
17. [推荐工作流](#17-推荐工作流)
18. [性能与并行](#18-性能与并行)
19. [退出码](#19-退出码)
20. [常见问题与故障排查](#20-常见问题与故障排查)
21. [安全检查清单](#21-安全检查清单)

---

## 1. PrivacyFS 是什么

PrivacyFS 是一个面向 AI 工具的本地文件系统隐私保护工具。它可以：

- 扫描目录并输出经过化名处理的文件清单；
- 检测多个文件、目录和层级组合形成的身份推断风险；
- 自动泛化日期、地点、学校和职业等准标识符；
- 将高风险医疗或法律目录压缩为空占位目录；
- 创建供 Claude Code、Codex 等 AI 工具使用的脱敏镜像；
- 清除 Office、PDF 和 JPEG 中常见的嵌入元数据；
- 使用 SQLite 保存真实表面与化名之间的稳定映射。

例如，原始目录：

```text
资料/
└── 张三/
    ├── 北京大学/
    ├── 2024-06-17住院记录.pdf
    └── 合同-13812345678.txt
```

普通脱敏清单可能显示为：

```text
资料/
└── PERSON_0001/
    ├── ORG_0001/
    ├── IDENT_0001记录.pdf
    └── 合同-PRIVATE_0001.txt
```

启用上下文强制策略后，高风险部分可能进一步显示为：

```text
资料/
└── PERSON_0001/
    ├── [LOCATION]大学/
    └── [MEDICAL_SUBTREE]/
```

PrivacyFS 做的是**假名化和最小披露**，不是不可逆匿名化。

---

## 2. 使用前必须理解的安全边界

### 2.1 PrivacyFS 可以保护什么

PrivacyFS 主要保护：

- 文件名和目录名中的姓名、机构、联系方式和敏感身份词；
- 整棵目录树中的日期、地点、学校、职业、医疗、法律等组合推断风险；
- 镜像中的路径名称；
- 使用 `--scrub-metadata` 时，部分格式中的作者、设备和位置元数据；
- 使用 `--context-mode enforce` 时，部分精确数量、大小、时间和目录结构信号。

### 2.2 PrivacyFS 默认不保护什么

PrivacyFS 默认不检查文件正文。以下内容仍可能泄漏：

- 文档正文中的姓名、地址、电话号码；
- 图片像素中出现的人脸、证件或文字；
- 音视频内容；
- 不支持格式中的嵌入元数据；
- AI 已经从其他渠道获得的信息；
- 人为拆分或变形的敏感词，例如 `R-1-8`；
- 检测模型和规则未覆盖的隐含关系。

即使启用了组合风险策略，也不能证明任意 AI 在拥有无限公开背景知识时绝对无法推断身份。

### 2.3 硬链接写穿风险

`mirror` 默认使用硬链接。硬链接镜像与原件共享相同文件内容：

```text
AI 修改镜像文件 = 原始文件也被修改
```

如果 AI 需要写文件，必须使用：

```powershell
privacyfs mirror D:\资料 E:\AI视图 --copy
```

### 2.4 映射数据库是敏感资产

`mapping.db` 保存真实表面与化名之间的对应关系。它相当于解密密钥：

- 不要提交到 Git；
- 不要发送给 AI 或第三方；
- 不要放入共享目录；
- 建议使用操作系统权限或磁盘加密保护；
- 自定义数据库位于扫描目录中时，PrivacyFS 会尝试自动排除它，但仍建议放在目录外部。

### 2.5 合规含义

PrivacyFS 输出通常仍属于个人信息，因为映射表和背景信息可能恢复身份。使用者仍需根据 PIPL、GDPR 或所在地区法规评估处理和共享行为。

---

## 3. 安装

### 3.1 环境要求

- Python 3.10 或更高版本；
- 推荐 Python 3.11 或 3.12；
- Windows 是当前重点支持平台；
- 安装命令所在 Python 的 `Scripts` 目录需要加入 `PATH`。

### 3.2 从项目目录安装

```powershell
cd C:\PrivacyFS
pip install -e .
```

验证：

```powershell
privacyfs --help
```

如果提示找不到命令，可以使用：

```powershell
py -3.11 -m privacyfs.cli --help
```

或检查 Python Scripts 目录是否在 `PATH` 中。

### 3.3 安装开发依赖

```powershell
pip install -e ".[dev]"
```

运行测试：

```powershell
py -3.11 -m pytest tests/ -q
```

### 3.4 安装 HanLP

```powershell
pip install hanlp
```

HanLP 会带来 Torch 等较大依赖，并在首次使用时下载约 114 MB 的模型。

如果自动下载失败，可以手动放置模型：

```powershell
curl -sL -o $env:TEMP\mtl.zip "https://file.hankcs.com/hanlp/mtl/close_tok_pos_ner_srl_dep_sdp_con_electra_small_20210111_124159.zip"
mkdir ~\.hanlp\mtl
cd ~\.hanlp\mtl
tar -xf $env:TEMP\mtl.zip
```

已知兼容组合：

```text
torch >= 2.5
transformers == 4.46.x
```

PrivacyFS 不需要 `torchaudio`。旧版 `torchaudio` 如果造成加载错误，可以考虑卸载。

---

## 4. 快速开始

### 4.1 扫描当前目录

```powershell
privacyfs
```

等同于：

```powershell
privacyfs scan .
```

### 4.2 扫描指定目录

```powershell
privacyfs scan D:\资料
```

裸路径也会被当作 `scan`：

```powershell
privacyfs D:\资料
```

### 4.3 递归扫描

```powershell
privacyfs scan D:\资料 -r
```

限制为两层：

```powershell
privacyfs scan D:\资料 -r 2
```

### 4.4 审计组合推断风险

```powershell
privacyfs analyze D:\资料
```

### 4.5 生成自动处理后的 JSON

```powershell
privacyfs scan D:\资料 -r --context-mode enforce -f json -o safe-tree.json
```

### 4.6 创建写隔离镜像

```powershell
privacyfs mirror D:\资料 E:\AI视图 --copy --context-mode enforce
```

如果文档可能包含作者或设备元数据：

```powershell
privacyfs mirror D:\资料 E:\AI视图 --scrub-metadata --context-mode enforce
```

---

## 5. 核心概念

### 5.1 表面

“表面”是检测器从路径中识别出的原始字符串，例如：

```text
张三
北京大学
13812345678
住院
```

### 5.2 化名

不同类型使用不同前缀：

```text
PERSON_0001
ORG_0001
PRIVATE_0001
IDENT_0001
ROLE_0001
SENSITIVE_0001
```

同一 `mapping.db` 中，同一类别和表面会复用化名。新的人物分配统一 PERSON/NAME_LIST；旧库中已有不同代号的记录保留，不自动重编号。实际跨发布化名作用域仍未实现。

### 5.3 准标识符

准标识符单独看可能不敏感，但组合后可能定位个人，例如：

```text
城市 + 学校 + 专业 + 年份
机构 + 职位 + 地点
精确日期 + 地点 + 医疗事件
```

### 5.4 主体

上下文分析需要先判断哪些路径可能属于同一个主体。

默认 `top_level_directory` 模式：

- 每个一级目录先作为一个主体；
- 根目录中的文件先分别作为独立主体；
- 明确检测到同一个人物或自定义名称时，可以跨一级目录合并。

`whole_release` 模式会把整批数据当作一个主体，适合整体风险警示，但不适合比较多个主体的 `k` 支持度。

### 5.5 k 支持度

如果某个准标识符组合只出现在一个主体上，它通常比出现在多个主体上更容易被重新识别。

默认：

```yaml
k_anonymity: 3
```

表示组合至少应由 3 个主体共享。当前实现使用可解释的组合支持度规则，不代表完整的统计匿名化证明。

### 5.6 审计与强制执行

- `audit`：发现风险并报告，但不修改输出；
- `enforce`：自动泛化或抑制，并重新评估；
- `off`：完全不运行上下文策略。

---

## 6. `scan`：生成脱敏清单

### 6.1 基本语法

```powershell
privacyfs scan [目录] [选项]
```

目录省略时使用当前目录。

### 6.2 递归深度

| 命令 | 行为 |
|---|---|
| `privacyfs scan D:\资料` | 默认只列一层 |
| `privacyfs scan D:\资料 -r` | 无限递归 |
| `privacyfs scan D:\资料 -r 2` | 最多两层 |
| `privacyfs scan D:\资料 --no-size` | 不输出大小，浅层扫描可避免全树大小统计 |

默认需要计算目录递归大小，因此即使只显示一层，也可能遍历更深层目录。加 `--no-size` 后，浅层扫描可以更快。

### 6.3 输出格式

Tree：

```powershell
privacyfs scan D:\资料 -f tree
```

JSON：

```powershell
privacyfs scan D:\资料 -r -f json
```

写入文件：

```powershell
privacyfs scan D:\资料 -r -f json -o listing.json
```

超过 50,000 个可见条目时，Tree 输出会被拒绝，应使用 JSON。

### 6.4 完整选项

| 选项 | 默认值 | 说明 |
|---|---:|---|
| `PATH` | `.` | 扫描目录 |
| `-r, --recursive [N]` | 一层 | 不带数值表示无限递归；带数值表示最大层数 |
| `-f, --format` | `tree` | `tree` 或 `json` |
| `-o, --output` | stdout | 写入文件 |
| `--rules` | 无 | YAML 规则文件 |
| `--ner` | 规则文件值，否则 `auto` | `auto`、`hanlp`、`jieba`、`off` |
| `--no-ner` | false | 关闭中文 NER |
| `--no-org` | false | 关闭机构检测 |
| `--no-professions` | false | 关闭职业检测 |
| `--no-en-names` | false | 关闭英文姓名检测 |
| `--no-pinyin` | false | 关闭拼音姓名检测 |
| `--no-identity` | false | 关闭身份关联词检测 |
| `--llm` | false | 启用 Ollama 补充检测 |
| `--llm-model` | 配置值 | 指定 Ollama 模型 |
| `--llm-url` | 配置值 | 指定 Ollama 地址 |
| `--paranoid` | false | 追加 QQ、银行卡等高误报规则 |
| `--no-size` | false | 不显示大小 |
| `-j, --jobs` | `1` | 工作进程数；`0` 表示 CPU 自动值 |
| `--context-mode` | `off` | `off`、`audit`、`enforce` |
| `--write-ignore` | false | 向真实目录写入 `.ignore`，慎用 |
| `--db` | 默认数据库 | 指定映射数据库 |

### 6.5 `--write-ignore`

该选项会把敏感文件名写入真实目录中的 `.ignore`，使遵守 ignore 规则的工具跳过它们。

```powershell
privacyfs scan D:\资料 --write-ignore
```

这是少数会修改真实扫描目录的操作之一。使用前建议备份已有 `.ignore`。

---

## 7. `analyze`：组合推断风险审计

### 7.1 基本语法

```powershell
privacyfs analyze [目录] [选项]
```

`analyze` 始终递归分析完整可见目录树，不修改被扫描目录。

### 7.2 Tree 报告

```powershell
privacyfs analyze D:\资料
```

示例：

```text
PrivacyFS context audit
42 entries, 5 subjects, 2 risks, k=3

[HIGH] medical_reidentification (RISK_...)
  subjects: SUBJECT_...
  entries: ENTRY_..., ENTRY_...
  signals: DATE, LOCATION, MEDICAL
  support: 1
  reason: 医疗事件与多个准标识符共同出现，可能重新识别主体。
  actions: generalize_date, generalize_location, suppress_subtree
```

报告不会打印真实路径、真实表面或稳定跨报告 ID。

### 7.3 JSON 报告

```powershell
privacyfs analyze D:\资料 -f json -o risk-report.json
```

JSON 包含：

- 条目数；
- 主体数；
- 风险数；
- 高风险数；
- `k` 值；
- 风险类型；
- 安全主体 ID 和条目 ID；
- 支持度；
- 建议动作。

### 7.4 覆盖 k 值

```powershell
privacyfs analyze D:\资料 -k 5
```

命令行值只作用于本次审计。

### 7.5 完整选项

| 选项 | 默认值 | 说明 |
|---|---:|---|
| `PATH` | `.` | 要分析的目录 |
| `--rules` | 无 | YAML 规则文件 |
| `-f, --format` | `tree` | `tree` 或 `json` |
| `-o, --output` | stdout | 写入报告文件 |
| `-k, --k-anonymity` | 规则值 | 覆盖组合支持度阈值 |
| `--ner` | 规则文件值，否则 `auto` | NER 引擎 |
| `--llm` | false | 使用本地 LLM 检查未命中名称 |
| `--llm-model` | 配置值 | Ollama 模型 |
| `--llm-url` | 配置值 | Ollama 地址 |
| `-j, --jobs` | `1` | 并行度 |
| `--db` | 无 | 启用 LLM 时使用的判定缓存数据库 |

### 7.6 可见目录树的含义

以下内容不会进入分析：

- 被 `excludes` 排除的目录；
- 符号链接和 junction 指向的内容；
- 被 `hidden_keywords` 命中的目录内部条目。

隐藏目录本身仍会作为 `[HIDDEN]` 类信号存在，但其内部组合关系不可见。

---

## 8. 上下文策略模式

`scan` 和 `mirror` 支持：

```powershell
--context-mode off|audit|enforce
```

### 8.1 `off`

```powershell
privacyfs scan D:\资料 --context-mode off
```

- 默认模式；
- 保持传统单路径检测和化名行为；
- 不构建主体关系；
- 不执行组合风险处理。

### 8.2 `audit`

```powershell
privacyfs scan D:\资料 -r --context-mode audit
```

- 使用完整可见树计算风险；
- 在 stderr 输出风险数量摘要；
- 正常清单与 `off` 模式保持一致；
- 不自动泛化或隐藏额外内容。

建议在正式启用 `enforce` 前先运行 audit，了解受影响范围。

### 8.3 `enforce`

```powershell
privacyfs scan D:\资料 -r --context-mode enforce
```

执行顺序：

```text
完整可见树检测
→ 主体聚类
→ 组合风险评估
→ 泛化或抑制
→ 重新分析变换后的视图
→ 继续处理或拒绝输出
```

可能产生：

```text
[DATE]
[LOCATION]
[EDUCATION_ORG]
[MEDICAL_SUBTREE]
[LEGAL_SUBTREE]
[PATH]
```

如果达到 `max_policy_passes` 后仍有高风险：

- 命令以退出码 3 结束；
- 不写目标输出文件；
- 不创建镜像；
- 不会只打印警告后继续发布。

### 8.4 浅层输出仍做完整风险分析

即使执行：

```powershell
privacyfs scan D:\资料 -r 1 --context-mode enforce
```

策略仍先分析完整可见树，然后才按显示深度裁剪输出。不能通过浅层显示绕过风险分析。

### 8.5 统计和结构模糊

默认：

```yaml
context:
  blur_structure: true
  blur_stats: true
```

`blur_structure`：

- 将长的单子目录链压缩为 `[PATH]`；
- 降低精确层级结构泄漏；
- 不影响统计模糊开关。

`blur_stats`：

- 将文件数和目录数转换为区间；
- 将文件大小转换为区间；
- 镜像 marker 不记录精确创建时间；
- 镜像 marker 和 CLI 使用区间统计。

静态清单中的可见条目仍然可以被逐项计数。完全隐藏访问规模需要动态按需访问层，当前静态版本不提供这一保证。

---

## 9. `mirror`：生成脱敏镜像

### 9.1 基本语法

```powershell
privacyfs mirror 源目录 目标目录 [选项]
```

示例：

```powershell
privacyfs mirror D:\资料 E:\AI视图
```

### 9.2 硬链接模式

默认优先硬链接：

```powershell
privacyfs mirror D:\资料 E:\AI视图
```

优点：

- 几乎不额外占用文件内容空间；
- 创建速度快；
- 同卷文件通常可以直接链接。

风险：

- 镜像和原件共享内容；
- AI 修改镜像会修改原件；
- 内容和未清理元数据完全相同。

跨卷或权限不允许硬链接时，工具会尝试回退复制。

### 9.3 复制模式

```powershell
privacyfs mirror D:\资料 E:\AI视图 --copy
```

适合：

- AI 需要修改文件；
- 希望保护原始文件内容不被写穿；
- 源和目标位于不同磁盘。

复制模式仍不会自动清理所有元数据，除非同时使用 `--scrub-metadata`。

### 9.4 元数据清理模式

```powershell
privacyfs mirror D:\资料 E:\AI视图 --scrub-metadata
```

该选项自动使用真实复制，并处理：

| 格式 | 清理内容 |
|---|---|
| DOCX/XLSX/PPTX | 标题、主题、作者、描述、关键词、最后修改者、公司、Manager、自定义属性等 |
| PDF | `/Info` 作者、创建者、标题、主题、关键词及 XMP |
| JPG/JPEG | EXIF，包括 GPS、设备、作者等 |

文件和目录时间戳会尝试统一为：

```text
2000-01-01 00:00:00
```

无法识别或解析失败的格式会原样复制，并计入 `plain-copied`，不会阻止整个镜像任务。

### 9.5 组合风险强制处理

```powershell
privacyfs mirror D:\资料 E:\AI视图 --copy --context-mode enforce
```

建议把 `--copy` 和 `--context-mode enforce` 一起用于 AI 工作目录。

医疗和法律高风险子树会变成空目录：

```text
[MEDICAL_SUBTREE]/
[LEGAL_SUBTREE]/
```

被压缩的原始文件不会被复制或硬链接。

### 9.6 重建镜像

```powershell
privacyfs mirror D:\资料 E:\AI视图 --force
```

安全限制：

- 目标不得等于源目录；
- 目标不得位于源目录内部；
- 源目录不得位于目标目录内部；
- 非空目标必须包含有效 JSON 的 `.privacyfs-mirror` 标记，且 `created_by` 为 `privacyfs`；
- 没有 marker 的目录不会被 `--force` 删除。

镜像不执行增量更新。源目录变化后使用 `--force` 全量重建。先构建同级暂存目录再切换；复制或切换失败会保留旧视图或备份，返回非零状态。构建期间请保持源树静止。

同级 `.privacyfs-lock-*` 文件用于进程锁；`.privacyfs-transaction-*`、`.privacyfs-build-*` 和 `.privacyfs-backup-*` 用于切换恢复。再次针对同一目标运行会先检查恢复状态；状态不一致时拒绝并保留数据，不要直接删除备份。标记和日志不是对同权限恶意进程的密码学所有权证明，也不保证操作系统掉电后的事务持久性。

### 9.7 完整选项

| 选项 | 默认值 | 说明 |
|---|---:|---|
| `PATH` | 必填 | 源目录 |
| `DEST` | 必填 | 目标目录 |
| `--rules` | 无 | YAML 规则文件 |
| `--ner` | 规则文件值，否则 `auto` | NER 引擎 |
| `--no-ner` | false | 关闭 NER |
| `--no-org` | false | 关闭机构检测 |
| `--no-professions` | false | 关闭职业检测 |
| `--no-en-names` | false | 关闭英文姓名检测 |
| `--no-pinyin` | false | 关闭拼音姓名检测 |
| `--no-identity` | false | 关闭身份关联词检测 |
| `--llm` | false | 启用 Ollama |
| `--llm-model` | 配置值 | Ollama 模型 |
| `--llm-url` | 配置值 | Ollama 地址 |
| `--paranoid` | false | 追加高误报正则 |
| `--db` | 默认数据库 | 映射数据库 |
| `--copy` | false | 强制复制，避免硬链接写穿 |
| `-j, --jobs` | `1` | 并行度 |
| `--context-mode` | `off` | `off`、`audit`、`enforce` |
| `--scrub-metadata` | false | 清理支持格式的嵌入元数据 |
| `--force` | false | 重建已有的 PrivacyFS 镜像 |

---

## 10. `tui`：交互式浏览

```powershell
privacyfs tui D:\资料 -r
```

TUI 提供：

- 名称和大小双列显示；
- CJK 宽度对齐；
- 终端缩放适配；
- 上下键浏览；
- `q` 退出；
- 后台扫描进度；
- 退出时取消扫描和检测 worker。

选项：

| 选项 | 说明 |
|---|---|
| `PATH` | 浏览目录，默认当前目录 |
| `-r, --recursive [N]` | 无限递归或限制层数 |
| `--rules` | 规则文件 |
| `--ner` | NER 引擎 |
| `--llm` | 使用 Ollama |
| `--llm-model` | Ollama 模型 |
| `--llm-url` | Ollama 地址 |
| `-j, --jobs` | 并行度 |
| `--db` | 映射数据库 |

当前 TUI 不提供 `--context-mode`。需要组合风险处理时，先使用 `analyze`，或通过 `scan/mirror --context-mode enforce` 生成结果。

---

## 11. `map`：管理化名映射

### 11.1 默认位置

优先顺序：

1. `PRIVACYFS_HOME` 环境变量；
2. `%LOCALAPPDATA%`；
3. 用户主目录下的 `.privacyfs`。

默认数据库文件名：

```text
PrivacyFS\mapping.db
```

### 11.2 导出映射

推荐写入文件：

```powershell
privacyfs map show -o mapping.json
```

指定数据库：

```powershell
privacyfs map show --db D:\private\mapping.db -o mapping.json
```

不推荐直接打印：

```powershell
privacyfs map show
```

真实名称会留在终端滚动记录中。

### 11.3 清空映射

```powershell
privacyfs map reset --yes
```

指定数据库：

```powershell
privacyfs map reset --db D:\private\mapping.db --yes
```

清空后：

- 旧化名可能无法再对应到真实表面；
- 后续扫描会重新从 `0001` 等编号生成；
- 旧输出和新输出中的相同编号可能代表不同对象；
- LLM 判定缓存也会被清空。

执行前应确认不再需要恢复旧映射关系。

---

## 12. 规则文件

### 12.1 使用方式

```powershell
privacyfs scan D:\资料 --rules C:\private\privacy-rules.yaml
privacyfs analyze D:\资料 --rules C:\private\privacy-rules.yaml
privacyfs mirror D:\资料 E:\AI视图 --rules C:\private\privacy-rules.yaml
```

规则文件可能直接包含真实姓名、公司和项目代号，必须放在扫描目录和代码仓库之外。

### 12.2 完整示例

```yaml
literal_names:
  - 张三
  - 李四
  - 某某公司
  - 内部项目代号

keywords:
  - 机密
  - 内部资料

regexes:
  - '(?<!\d)\d{19}(?!\d)'

hidden_keywords:
  - 私密

excludes:
  - node_modules
  - '*.cache'

org_suffixes:
  - 实验室

professions:
  - 机长

identity_terms:
  - 内部番号

pinyin_allowlist:
  - myproject

use_ner: true
ner_engine: auto
detect_orgs: true
detect_professions: true
detect_en_names: true
detect_pinyin: true
detect_identity: true
paranoid: false

use_llm: false
llm_model: privacyfs-minicpm
llm_url: http://127.0.0.1:11434
llm_max: 500

context:
  enabled: true
  mode: audit
  subject_scope: top_level_directory
  k_anonymity: 3
  max_policy_passes: 4
  fail_closed: true
  blur_structure: true
  blur_stats: true
```

### 12.3 列表字段行为

以下列表会在内置默认值上追加，而不是替换默认值：

- `keywords`
- `regexes`
- `literal_names`
- `org_suffixes`
- `professions`
- `identity_terms`
- `hidden_keywords`
- `excludes`
- `pinyin_allowlist`

### 12.4 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `literal_names` | 字符串列表 | 明确姓名、机构、项目代号，可靠性最高 |
| `keywords` | 字符串列表 | 额外敏感词 |
| `regexes` | 字符串列表 | 额外正则表达式 |
| `hidden_keywords` | 字符串列表 | 命中目录后不遍历其内部 |
| `excludes` | 字符串列表 | glob 风格排除项，不报告也不统计 |
| `org_suffixes` | 字符串列表 | 机构后缀 |
| `professions` | 字符串列表 | 职业和职务 |
| `identity_terms` | 字符串列表 | 身份、经历和状态关联词 |
| `pinyin_allowlist` | 字符串列表 | 拼音检测豁免词 |
| `use_ner` | 布尔值 | 是否启用中文 NER |
| `ner_engine` | 字符串 | `auto`、`hanlp`、`jieba`、`off` |
| `detect_orgs` | 布尔值 | 机构检测 |
| `detect_professions` | 布尔值 | 职业检测 |
| `detect_en_names` | 布尔值 | 英文姓名检测 |
| `detect_pinyin` | 布尔值 | 拼音姓名检测 |
| `detect_identity` | 布尔值 | 身份关联词检测 |
| `paranoid` | 布尔值 | QQ 和银行卡类高误报规则 |
| `use_llm` | 布尔值 | 本地 LLM 补充检测 |
| `llm_model` | 非空字符串 | Ollama 模型名 |
| `llm_url` | 非空字符串 | Ollama 基础 URL |
| `llm_max` | 非负整数 | 每次扫描最多发送的未命中名称数 |

### 12.5 Context 字段

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `enabled` | `true` | 是否允许上下文分析 |
| `mode` | `audit` | `analyze` 配置；`scan/mirror` 仍由命令行 `--context-mode` 指定 |
| `subject_scope` | `top_level_directory` | `top_level_directory` 或 `whole_release` |
| `k_anonymity` | `3` | 组合支持度阈值 |
| `max_policy_passes` | `4` | 最大自动处理轮数 |
| `fail_closed` | `true` | 库级策略残留风险时抛出错误；CLI enforce 始终拒绝残留高风险 |
| `blur_structure` | `true` | 压缩单子目录链 |
| `blur_stats` | `true` | 桶化数量、大小和镜像 marker |

### 12.6 类型校验

布尔字段必须使用 YAML 布尔值：

```yaml
use_ner: false       # 正确
use_ner: 'false'     # 错误：字符串
```

非法正则、未知规则或 Context 字段、负数 `llm_max` 和非正整数 `k_anonymity` 会在启动时被拒绝。

NER 优先级为显式 CLI > YAML > 默认值；`--no-ner` 优先关闭。YAML 的关闭值写为 `ner_engine: 'off'`。机构（特别是无后缀名称）放入 `literal_orgs`；旧 `literal_names` 中带机构后缀的值会按机构处理，不提供人物合并依据。

`llm_revision: "1"` 标识同名模型的权重版本，由用户在换权重时递增。LLM 缓存同时区分服务地址、模型名、修订、提示词和解析协议；旧无版本缓存保留但不复用。未知配置字段不再被静默忽略。

---

## 13. 检测能力

| 类型 | 示例 | 默认化名/处理 |
|---|---|---|
| 敏感关键词 | R18、NSFW、病历、判决书 | `SENSITIVE_` |
| 中文姓名 | 张三、王富贵 | `PERSON_` |
| 英文姓名 | John Smith | `PERSON_` |
| 拼音姓名 | ZhangSan、San Zhang | `PERSON_` |
| 机构 | 某某公司、北京大学、Acme Inc | `ORG_` |
| 职业职务 | 律师、经理、CEO | `ROLE_` |
| 身份关联词 | 奖学金、住院、房贷、入党 | `IDENT_` |
| 手机号、身份证、邮箱 | `138...`、身份证号、邮箱 | `PRIVATE_` |
| QQ、银行卡类数字 | `--paranoid` 时检测 | `PRIVATE_` |
| 敏感目录 | R18、NSFW 等 | `[HIDDEN]` |
| 精确日期 | 年/月/日 | Context 准标识符 |
| 地点 | 省、市、区、县 | Context 准标识符 |
| 学校、专业、奖项 | 教育经历组合 | Context 准标识符 |
| 医疗、法律、金融事件 | 组合风险 | 泛化或敏感子树占位 |

关键词会生成部分 NFKC、全角和简繁体变体。检测仍可能存在误报或漏报，自定义 `literal_names` 和规则是最可靠的补充手段。

2026-09-18起，拼音启发式不再把完整英文词LICENSE/LICENCE、cache、module等直接当作姓名；Li Ne/LiNe这类分隔或驼峰拼音仍可检出，名单可明确指定与英文单词重合的真实姓名。豁免只影响拼音候选，不跳过整个文件、目录或其他检测器。

`professions`和`identity_terms`中的纯英文词/短语（包括自定义项及全角变体）不再匹配紧邻其他英文字母的子串，例如cookie中的COO、coffers中的offer。下划线、空格、数字和中文可作为边界，`COO_salary.csv`、`个人VİSA.pdf`仍命中；无分隔的`visaReport`不再仅凭visa片段命中。`keywords`、`literal_names`、`literal_orgs`保持显式子串匹配，需要指定这种片段时可使用这些明确规则。相关回归用例见 `tests/test_filename_false_positives.py`。

---

## 14. 组合风险与自动处理

### 14.1 当前风险类型

#### 身份三角定位

```text
LOCATION + ORG + ROLE
```

可能动作：

```text
generalize_location
generalize_role
break_linkage
```

#### 教育经历重识别

```text
SCHOOL + MAJOR + DATE
```

可能动作：

```text
generalize_school
generalize_date
break_linkage
```

#### 精确事件关联

```text
DATE_DAY + LOCATION + EVENT
```

可能动作：

```text
generalize_date
generalize_location
redact_event
```

#### 医疗重识别

医疗事件同时出现至少两个以下准标识符：

```text
DATE
LOCATION
ORG
ROLE
SCHOOL
MAJOR
AWARD
```

可能动作：

```text
generalize_date
generalize_location
suppress_subtree
```

#### 法律事件关联

```text
LEGAL + LOCATION + DATE
```

可能动作：

```text
generalize_date
generalize_location
suppress_subtree
```

#### 跨目录实体关联

同一明确实体出现在多个一级目录时，会提示跨目录关联。当前此类风险通常为中等级别，主要建议后续使用发布级化名或断链策略。

### 14.2 自动处理的单调性

策略从保留信息最多的形式逐步向更安全形式移动：

```text
精确值 → 泛化值 → 类型占位 → 子树占位/删除
```

不会在后续轮次重新恢复已经删除的精度。

### 14.3 占位目录

普通隐藏规则：

```text
[HIDDEN]
[HIDDEN]__2
```

组合风险策略：

```text
[MEDICAL_SUBTREE]
[LEGAL_SUBTREE]
```

普通文件、目录及隐藏占位符统一消歧，目录的后代同步改名。源树含 `.privacyfs-mirror` 会分配其他目标名，内部标记不会写到源硬链接。清单与镜像使用同一映射。

---

## 15. 输出格式

### 15.1 Tree

适合人工查看：

```text
资料
├── PERSON_0001
│   ├── ORG_0001
│   └── [MEDICAL_SUBTREE] (content hidden)
└── 普通文件.txt
```

终端输出会根据宽度截断长名称；写入文件时不按终端宽度截断。

### 15.2 JSON

普通模式示例：

```json
{
  "root": "资料",
  "stats": {
    "files": 10,
    "dirs": 4,
    "hidden_dirs": 1
  },
  "entries": [
    {
      "path": "PERSON_0001/普通文件.txt",
      "type": "file",
      "size": 1200
    }
  ]
}
```

Enforce 且 `blur_stats: true` 时：

```json
{
  "stats": {
    "files": "10-19",
    "dirs": "1-4",
    "hidden_dirs": "1-4"
  },
  "entries": [
    {
      "path": "PERSON_0001/[MEDICAL_SUBTREE]",
      "type": "dir",
      "size": "1-10 MB",
      "hidden": true
    }
  ]
}
```

不要编写依赖 `size` 永远为整数的下游程序；Enforce 统计模糊模式下它是区间字符串。

### 15.3 stdout 与 stderr

- 清单和 JSON 写入 stdout；
- 进度、警告、风险摘要和成功提示写入 stderr；
- 使用 shell 重定向时可以分别保存。

PowerShell 示例：

```powershell
privacyfs scan D:\资料 -f json 1>listing.json 2>scan.log
```

---

## 16. 本地 LLM 与 HanLP

### 16.1 NER 引擎

| 值 | 行为 |
|---|---|
| `auto` | HanLP 可用时使用 HanLP+jieba，否则使用 jieba |
| `hanlp` | 优先 HanLP；不可用时回退 jieba |
| `jieba` | 只使用 jieba 人名检测 |
| `off` | 关闭中文 NER，其他规则仍工作 |

禁用所有 NER：

```powershell
privacyfs scan D:\资料 --no-ner
```

### 16.2 Ollama

创建模型示例：

```powershell
ollama create privacyfs-minicpm -f Modelfile
```

`Modelfile`：

```text
FROM C:/path/to/MiniCPM5-1B-Q4_K_M.gguf
```

启用：

```powershell
privacyfs analyze D:\资料 --llm
```

指定模型和地址：

```powershell
privacyfs analyze D:\资料 `
  --llm `
  --llm-model privacyfs-minicpm `
  --llm-url http://127.0.0.1:11434
```

只有前置规则均未命中的名称才会送给 LLM。实际解析成功的判定会写入缓存；失败、取消或无法解析的名称不会永久缓存为安全。

### 16.3 网络隐私提醒

默认 `llm_url` 指向本机。若将它改为远程地址，未命中的文件名会发送到该远程服务，运行时将不再是完全本地处理。只有在确认接收方可信并符合数据政策时才这样配置。

HanLP 首次下载模型也需要网络，但模型加载后的目录检测在本机执行。

---

## 17. 推荐工作流

### 17.1 只把目录结构交给 AI

```powershell
privacyfs analyze D:\资料 -f json -o risk.json
privacyfs scan D:\资料 -r --context-mode enforce -f json -o safe-listing.json
```

人工检查 `risk.json` 和 `safe-listing.json` 后，只把安全清单提供给 AI。

### 17.2 让 AI 读取但不修改文件

```powershell
privacyfs mirror D:\资料 E:\AI视图 `
  --context-mode enforce `
  --scrub-metadata
```

把 AI 工作目录设置为 `E:\AI视图`，不要同时授权真实源目录。

### 17.3 让 AI 修改文件

```powershell
privacyfs mirror D:\资料 E:\AI工作区 `
  --copy `
  --context-mode enforce `
  --scrub-metadata
```

AI 修改完成后，人工检查差异，再决定是否复制回真实目录。

### 17.4 调试误报

1. 使用 `analyze -f json` 查看风险类别；
2. 使用 `scan --context-mode audit` 对比普通清单；
3. 根据需要增加 `pinyin_allowlist`；
4. 关闭单个高误报检测层，而不是一次关闭所有保护；
5. 为明确安全项目名添加豁免前，先确认它不会代表真人姓名。

### 17.5 定期重建镜像

```powershell
privacyfs mirror D:\资料 E:\AI视图 `
  --copy `
  --context-mode enforce `
  --force
```

Mirror 不做增量 diff。

---

## 18. 性能与并行

### 18.1 参考数据

已有参考：

| 规模/模式 | 参考耗时 |
|---|---:|
| 20,000 文件，NER off | 约 2 秒 |
| 20,000 文件，jieba | 约 7 秒 |
| 20,000 文件，HanLP | 约 25 秒，含模型加载 |
| 约 370 万条目 | 约 8 分钟 |

实际性能取决于磁盘、文件系统、模型缓存、路径唯一性和规则数量。

### 18.2 并行设置

```powershell
privacyfs scan D:\资料 -j 4
privacyfs analyze D:\资料 -j 0
```

- `-j 1`：默认串行；
- `-j N`：指定并行度；
- `-j 0`：使用 CPU 数量自动决定。

实现策略：

- 目录遍历使用线程池；
- 唯一名称达到 20,000 后，普通检测才可能启用进程池；
- HanLP 和 LLM 在主进程执行。

中小目录使用多进程可能比串行更慢。

### 18.3 Context 性能

上下文分析需要完整可见树、主体聚类和组合支持度计算，因此通常比普通浅层扫描更慢。项目包含：

```powershell
py -3.11 benchmarks/context_100k.py
```

用于测量 100,000 个合成条目的耗时和峰值内存。

---

## 19. 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 成功 |
| `2` | 参数、路径、规则文件、格式或镜像安全检查错误 |
| `3` | Context enforce 后仍有高风险，拒绝输出 |
| `4` | 文件系统、数据库、镜像构建或恢复失败；本次输出未确认 |
| 其他 | 未预期运行时错误或依赖错误 |

自动化脚本必须检查退出码：

```powershell
privacyfs scan D:\资料 -r --context-mode enforce -f json -o safe.json
if ($LASTEXITCODE -ne 0) {
    throw "PrivacyFS did not authorize the output"
}
```

不要在退出码非零时使用旧输出文件。建议每次运行前使用新的临时输出名，成功后再原子替换正式文件。

---

## 20. 常见问题与故障排查

### 20.1 找不到 `privacyfs` 命令

```powershell
py -3.11 -m privacyfs.cli --help
```

如果上述命令可用，说明 Python Scripts 目录未加入 `PATH`，或安装到了另一个 Python 环境。

### 20.2 `ModuleNotFoundError`

确认在正确解释器中安装：

```powershell
py -3.11 -m pip install -e .
py -3.11 -m privacyfs.cli --help
```

### 20.3 HanLP 下载或加载失败

尝试：

```powershell
privacyfs scan D:\资料 --ner jieba
```

或暂时关闭：

```powershell
privacyfs scan D:\资料 --ner off
```

检查 Torch、Transformers 和旧 `torchaudio` 版本冲突。

### 20.4 Ollama 不可用

PrivacyFS 会打印批次失败信息，并继续使用规则检测。确认：

```powershell
ollama list
ollama serve
```

以及：

```text
http://127.0.0.1:11434/api/chat
```

是否可访问。

失败项不会永久缓存为 `NONE`，服务恢复后可以重试。

### 20.5 `mirror already exists`

由 PrivacyFS 创建的镜像使用：

```powershell
privacyfs mirror D:\资料 E:\AI视图 --force
```

如果目标没有 `.privacyfs-mirror`，工具会拒绝清理。请不要手动伪造 marker；改用新的空目录。

### 20.6 镜像文件修改后原件也改变了

使用了默认硬链接模式。重新创建复制镜像：

```powershell
privacyfs mirror D:\资料 E:\AI工作区 --copy --force
```

### 20.7 Enforce 返回退出码 3

表示最大处理轮数后仍存在高风险。

建议：

1. 运行 `privacyfs analyze` 查看风险类别；
2. 增加更明确的 `literal_names`、`hidden_keywords`；
3. 提高敏感目录抑制范围；
4. 将数据拆成多个独立发布批次；
5. 不要简单关闭 `fail_closed` 期待 CLI 放行——CLI enforce 仍会拒绝残留高风险。

### 20.8 输出出现 `[PATH]`

`blur_structure: true` 会压缩长的单子目录链。关闭：

```yaml
context:
  blur_structure: false
  blur_stats: true
```

统计模糊可以独立保持开启。

### 20.9 JSON 中 size 变成字符串

这是 Enforce 统计模糊的预期行为：

```json
{"size": "10-100 MB"}
```

如果下游必须使用整数，可以关闭：

```yaml
context:
  blur_stats: false
```

关闭后会重新暴露精确大小和统计，应评估风险。

### 20.10 映射数据库被锁定

PrivacyFS 配置了 SQLite busy timeout，并能处理并发化名冲突。仍发生锁定时：

- 检查数据库是否位于网络盘；
- 避免长时间打开数据库的第三方 SQLite 工具；
- 确认目录可写；
- 为不同批处理使用不同数据库路径。

### 20.11 Tree 太大

改用 JSON：

```powershell
privacyfs scan D:\资料 -r -f json -o listing.json
```

### 20.12 为什么同一个敏感词有不同类型

不同实体类别仍可能产生不同类型代号。新人物分配统一 PERSON/NAME_LIST；旧库的两条历史代号保留。重叠替换按原文左侧优先、同起点最长的非重叠位置执行，只记录实际使用的代号。人物放 `literal_names`，机构放 `literal_orgs`。

---

## 21. 安全检查清单

### 安装和配置

- [ ] 使用受支持的 Python 环境；
- [ ] 规则文件放在扫描目录和 Git 仓库之外；
- [ ] `mapping.db` 位于受保护位置；
- [ ] 远程 `llm_url` 已经过明确安全评估；
- [ ] 使用严格 YAML 布尔值而不是字符串。

### 发布清单前

- [ ] 先运行 `privacyfs analyze`；
- [ ] 检查所有 High 风险；
- [ ] 使用 `--context-mode enforce` 生成正式输出；
- [ ] 检查命令退出码为 0；
- [ ] 不把 stderr 中的调试信息和映射导出一起发送；
- [ ] 人工抽查 Tree/JSON 中是否仍有可识别组合。

### 创建 AI 镜像前

- [ ] AI 需要写入时使用 `--copy`；
- [ ] 含 Office/PDF/JPEG 时考虑 `--scrub-metadata`；
- [ ] 使用 `--context-mode enforce`；
- [ ] 目标位于源目录之外；
- [ ] AI 没有同时获得真实源目录权限；
- [ ] 检查 `[MEDICAL_SUBTREE]`、`[LEGAL_SUBTREE]` 占位为空；
- [ ] 不将 `.privacyfs-mirror` 之外的目录交给 `--force`。

### 任务结束后

- [ ] 删除不再需要的镜像；
- [ ] 检查 AI 对复制镜像的修改；
- [ ] 保护或删除风险报告；
- [ ] 不随意清空仍需恢复的映射数据库；
- [ ] 如果输出已经外发，按个人信息而不是匿名数据管理。

---

## 附录 A：常用命令速查

```powershell
# 当前目录普通扫描
privacyfs

# 完整递归 JSON
privacyfs scan D:\资料 -r -f json -o listing.json

# 组合风险审计
privacyfs analyze D:\资料 -f json -o risk.json

# 只审计，不变换清单
privacyfs scan D:\资料 -r --context-mode audit

# 自动执行上下文策略
privacyfs scan D:\资料 -r --context-mode enforce -f json -o safe.json

# 写隔离镜像
privacyfs mirror D:\资料 E:\AI视图 --copy --context-mode enforce

# 元数据清理镜像
privacyfs mirror D:\资料 E:\AI视图 --scrub-metadata --context-mode enforce

# 重建已有 PrivacyFS 镜像
privacyfs mirror D:\资料 E:\AI视图 --copy --context-mode enforce --force

# TUI
privacyfs tui D:\资料 -r

# 安全导出映射
privacyfs map show -o mapping.json

# 清空映射
privacyfs map reset --yes
```

## 附录 B：相关文件

| 文件 | 用途 |
|---|---|
| `README.md` | 项目概览和快速使用 |
| `user_manual.md` | 本用户手册 |
| `rules.example.yaml` | 完整规则示例 |
| `EVALUATION_FRAMEWORK.md` | 效果与攻击评测的运行方法和边界 |
| `benchmarks/context_100k.py` | 上下文分析性能基准 |


**D0 安装与输出补充**

开发环境使用 `requirements-dev.lock` 约束和项目 `.venv`，命令见 README；当前验证结果以实际测试输出为准。数据库及其 -journal/-wal/-shm 旁文件也从库级扫描中排除。

CLI enforce 的根标签固定为 `[ROOT]`，避免未进入相对路径上下文的真实根名称被额外释放。镜像自身仍是普通文件目录，必须用外部权限/沙箱限制 AI 直接访问原盘。

Office 元数据清理按命名空间 URI 处理并验证指定字段，PDF 保留文档结构后移除文档级 /Info 与 XMP；不承诺清理正文、批注或所有嵌入对象。文件、目录和 marker 的 mtime/atime 在创建完成后冻结，不保证 Windows 创建时间被修改。无法解析/不支持格式依旧原样复制并计数；复制失败或时间冻结失败则阻止发布完整新视图。


**D1：显式正文规则检查**

新增 `privacyfs inspect FILE...`，仅对明确选择的 TXT/Markdown/CSV 文件做内存快照、字面解析、规则检测和来源定位。不会自动让 scan/analyze/mirror/tui 读取正文，不运行 NER/LLM，不访问 Markdown 链接、不执行 CSV 公式，也不写映射数据库。

默认识别 BOM，否则严格 UTF-8；历史中文编码可显式传 `--encoding gb18030`。文件读取/解析/检测失败、不支持或部分处理返回4，并在有效输入根的JSON中给出状态；finding_count为null，不能当作零风险。成功退出0也只代表声明范围内的处理完成，privacy_verified始终false。

位置为去除初始BOM后的Unicode码点左闭右开区间，保留原换行；物理行从1起，CSV逻辑行列从0起。原文接口只供可信本地代码使用，公开JSON不含路径、表面或内部摘要。具体格式、限额、API和内存清理边界见 `DOCUMENTS_D1.md`；阶段实施记录仅本地保存。
# D2 显式跨文件关系

使用 `privacyfs relate ROOT --config FILE` 分析显式配置的 TXT/Markdown/CSV 字段与编号关系。快速合成示例：

```powershell
privacyfs relate examples/d2 --config examples/d2/relations.json
```

输出为不含原始字段值和路径的安全ID报告，包含身份映射、编号连接及可选准标识符组合。`complete`只表示配置字段已处理，`privacy_verified`始终为false；0表示处理完成，2为参数/配置错误，4为读取/解析/模板/资源失败。正文关系不自动加入旧scan、analyze或mirror。

文本格式限定为一行一记录的`字段=值;字段=值`，不自动理解任意Markdown正文。名称相同仅产生候选，明确编号按类型和namespace隔离。证据回放与确认、拆分、拒绝使用活跃本地SDK会话；图与纠正不持久化，也不会写入旧mapping.db。详情含敏感数据，不能转发为普通公开报告。

配置、状态、切断点含义、限额及完整本地SDK示例见 [D2关系说明](RELATIONS_D2.md)。图上的候选切断点不代表文件已经脱敏；任务约束变换、输出验证及发布仍属于D3。

# D3 任务约束与本地发布

D3已实现独立的`prepare/review/export/release`工作流，基于D2显式模板和可信TaskProfile生成结构化副本。原始身份字段删除或化名、编号在本批保持一致，未配置列不带出；保留值需明确白名单，泛化需明确映射。

```powershell
privacyfs prepare examples/d3/statistics --relations examples/d3/statistics/relations.json --task-profile examples/d3/statistics/profile.json --state-dir .review/my-d3-state
```

使用返回的plan_id查看计划，以返回的approval_digest明确批准，再导出到与源/状态目录分离的容器。最终生成新的中性RELEASE目录，不覆盖旧发布。输入或配置改变须重新准备计划；中断后通过`release recover`核对现场。

私有状态目录用当前用户ACL保护，不能放在源树内，也不是Agent工作目录。`.privacyfs-state.json`为保留排除标记，旧遍历/镜像和正文入口不会处理该目录及后代；同名普通文件也会触发保守排除。

仅承诺声明的结构化字段、任务事实和已知残留检查，privacy_verified始终为false；不包含任意自然语言自动脱敏、GUI审核、持久实体纠正、跨接收方真实投递或运行时隔离。完整说明见 [D3发布文档](RELEASES_D3.md)。

# D4 工作区、持久纠正与增量

```powershell
privacyfs workspace init examples/d3/statistics --relations examples/d3/statistics/relations.json --task-profile examples/d3/statistics/profile.json --state-dir .review/my-d4-state
```

使用返回的workspace_id执行refresh，或在交互终端中使用`workspace review ID --local`查看证据和before/after。持久确认、拆分、拒绝、配置切换及LIFO撤销需要当前修订；源/版本变化后过期纠正不能直接用于发布。

使用`workspace prepare ID`生成包含持久纠正的D3计划，再按原review批准和export流程发布。普通prepare保持独立流程。默认复用未变内容和依赖分组，每次仍读取源字节核验；不会把缓存命中当作安全证明。

Windows索引、缓存、计划私有部分及密钥使用DPAPI保护；旧D3记录可逐条迁移并保留原批准绑定。可预览prune清理范围，或显式forget某个工作区；源文件、旧映射库和发布历史不会随之删除。

完整命令、默认无头安全输出、私有接口、配额、加密/恢复和平台限制见 [D4工作区说明](WORKSPACES_D4.md)。正文工作区仍无GUI审核、任意自由文本完整匿名化或运行时Agent隔离；后续新增的Slint GUI仅用于本地文件名排查。

# D5 有限文档试用版

0.2.0a1支持DOCX选定简单表格/段落和PDF显式页文本，保持PARTIAL外层状态，通过source_view指定选区后才可进入工作区/发布。新产物为CSV，不是完整原DOCX/PDF匿名化副本。修订、隐藏文本、域、复杂表格等可能直接拒绝；加密和无文本PDF不尝试密码或OCR。

使用`privacyfs sample NEW_DIRECTORY`创建纯合成材料，`privacyfs doctor --self-test --bundle diagnostics.zip`生成不含原文、路径、映射或模型地址的诊断包。均不会自动对外发送。Windows安装包以锁定wheel离线建立独立runtime，升级和卸载保留data及发布历史。

具体安装、审核和导出步骤见 [试用指南](TRIAL_D5.md)，格式和位置语义见 [支持矩阵](FORMATS_D5.md)。D5-07真实用户验证按用户明确安排延期，反馈模板尚待填写；不能把合成测试当作非开发者验收。

# 开发者效果与攻击评测

源码仓库增加 `python evals/run_effectiveness.py --split validation`，用于合成样本上的检测、处理对照、身份/属性推断及任务效用评测。加 `--include-cross-file` 同时报告现有D2冻结集指标。该入口不是 `privacyfs` 产品子命令，也未打进旧0.2.0a1试用包。

报告中的执行通过不代表无残余攻击；可用 `--require-no-residual-attacks` 显式设置门禁。没有运行模型、未完成攻击、产品拒绝均不能说成AI未能识别。Pi骨架和默认离线协议测试、数据范围与命令见 [评测体系](EVALUATION_FRAMEWORK.md)。

# Slint 本地名称GUI

工作台默认使用“NTFS 快速扫描（失败停止）”：对本地 NTFS 路径读取所在卷的元数据，选子目录时提取所选子树，先展示结构再启动 AI。权限不足时明确停止，可以关闭后手动运行 `PrivacyFS-Workbench-Admin.vbs` 请求 UAC 管理员启动。也可在“文件列表来源”改选普通遍历；兼容自动模式仍仅对卷根尝试 NTFS 并允许回退。硬链接名称会补齐，私有状态与重解析点继续过滤；不会读取正文或自动提权。此轮不包含跨次持久索引，详细范围见 [NTFS枚举说明](NTFS_ENUMERATION.md)。

独立工作台现在先加载并展示目录结构，再由用户点击“开始 AI 审阅”。模型使用 JSON Schema 约束的应用层工具协议，调用 `mark_privacy` 或 `record_clear`；每批校验后更新目录中的标签、颜色与理由，疑似隐私进入人工复核列表。未检查与本批未发现线索分别显示。默认 NTFS 预览尚未核验，可能缺少硬链接别名；目录索引读入后按需查询页面，不再全量整理后才显示。点击“核验并开始 AI 审阅”后复用索引并合并 USN 变动，再核验和更新显示的目录清单，随后才送给 AI；缺少检查点会提示重读，日志有缺口则停止。核验失败或取消不会把初步预览当作完整清单。完整操作、工具边界及历史会话范围见 [工作台说明](WORKBENCH_GUI.md)。

可选“目录 AI（本地模型）”在独立工作进程中直接加载所选GGUF，由模型按目录清单返回条目ID、隐私类别与理由。默认每批64条，按目录和清单长度拆分，同一扫描复用一次加载的模型；无需服务或HTTP，没有旧名称补充的500条总预算。缺失判断、超时及超长条目明确计入未检查，不当作安全。使用、依赖及真实模型验证限制见 [本地进程说明](EMBEDDED_AI_LLAMA_CPP.md)。

2026-09-18起，GUI默认不设遍历条目、命中数或待遍历目录数上限。结果分批暂存本机临时磁盘（Windows下DPAPI加密），每页仅加载80条；筛选中显示未完成状态，JSON导出逐批写入全部已保留命中。临时磁盘不足会失败，访问权限及检测覆盖限制仍适用。新扫描或关闭GUI会清理旧临时库；导出文件仍含真实路径，需要私有保存。重启GUI使用新版本，结果存储回归用例见 `tests/test_gui_scale.py`。

2026-09-19起，Slint专用界面和启动器仅本地保留，不纳入版本管理，新安装不再注册`privacyfs-gui`命令。仍保留本地界面源文件时，可双击 `PrivacyFS-GUI.vbs`，或安装可选依赖后运行 `python -m privacyfs.gui`。选择目录/C盘后手动开始，结果显示真实文件/子目录路径和命中理由，可搜索、按类型/类别筛选、分页、停止及导出本地JSON。工作台共用后端继续入库；详细范围与本地启动要求见 [GUI使用说明](GUI_SLINT.md)。旧D5安装ZIP未更新为GUI安装包。

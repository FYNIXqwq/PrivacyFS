# PrivacyFS

面向 AI 文件处理工作流的本地隐私辅助工具：检查文件名与目录上下文，生成化名清单或镜像，并在有限的结构化文档范围内提供关系分析、人工审核和验证发布。

**当前版本：`0.2.0a1` · Alpha / WIP · 状态更新：2026-09-19。** 核心 CLI 已有分阶段验证，桌面端和本地模型工作流仍在迭代。识别结果可能漏检或误报；化名输出不等于不可逆匿名化，镜像目录也不提供进程访问隔离。

[用户手册](user_manual.md) · [工作台说明](WORKBENCH_GUI.md) · [Wails 浏览器](wails-browser/README.md) · [第三方声明](THIRD_PARTY_NOTICES.md)

## 当前状态与 WIP

以下状态区分已实现的功能、仍在开发验证的部分，以及尚未启动的计划。历史实施报告中的测试数量只对应当时版本。

| 部分 | 状态 | 当前可用范围与未完成部分 |
|---|---|---|
| 核心 CLI、显式正文检查、关系与验证发布（D0–D4） | 已通过声明范围的阶段验收 | 名称扫描、上下文策略、有限字段关系、发布验证、持久纠正与增量工作区；不覆盖任意自然语言匿名化 |
| DOCX/PDF 与离线技术试用（D5-01～06） | 已交付技术试用版 | DOCX 简单段落/表格、PDF 指定页的文本投影到新 CSV；不保留原格式，不支持完整 Office/PDF 内容清理 |
| **Wails 3 目录浏览器** | **WIP：独立桌面入口** | 已实现选择目录、NTFS/普通遍历、目录树与列表虚拟滚动、全部展开/折叠和序号跳转；**尚无 AI、隐私分类、重命名或导出功能** |
| **Python AI 工作台** | **WIP：桌面工作流迭代** | Tk 工作台、NTFS 按需浏览、本地 GGUF 标记、人工复核与私有报告已实现；尚未接入 D4 正文证据审核/批准发布。Slint 名称排查界面仅本地保留，不纳入版本管理 |
| **本地模型效果与大规模体验** | **WIP：验证仍有限** | 有合成回归、协议 stub 和特定模型小规模冒烟；真实目录识别质量、整盘吞吐与 128K 满上下文资源需求仍需验证，不能由默认参数或合成基准推断 |
| 真人试点（D5-07 / 完整 G5） | 延期，未验收 | 按用户安排暂缓；技术试用包和合成测试不代表真人试用通过 |
| 受控 Agent 网关与运行时隔离（D6） | TODO，未启动 | 访问拦截、MCP 接入、原文授权及外发控制仍是后续计划 |
| 累计披露与语义风险校准（D7） | TODO，未启动 | 披露记录、组合增量与规则限额仍是研究计划，不提供数学隐私预算或校准泄露概率 |

Wails、Tk 工作台和 Slint 是独立入口，功能不能互相套用。已有 D5 离线包未随后续 GUI、工作台和 Wails 开发重新打包；使用这些新功能需要当前源码环境或自行构建。项目目前以 Windows 为主要验证平台，不宣称其它平台已完成同等验收。

Slint 专用界面代码、启动器及专用渲染测试已加入 `.gitignore`，从版本管理取得的源码不包含该界面。`src/privacyfs/gui/` 中的扫描、结果存储和模型后端仍由工作台使用，继续纳入源码。Slint 历史文档保留供本地实验参考。

## 选择入口

| 需求 | 入口 | 说明 |
|---|---|---|
| 浏览目录结构，无需模型 | `PrivacyFS-Wails.vbs` | [Wails 浏览器](wails-browser/README.md)，需先构建 Go 程序 |
| 目录预览 → AI 标记 → 人工复核 | `PrivacyFS-Workbench.vbs` / `privacyfs-workbench` | [Python 工作台](WORKBENCH_GUI.md)，使用 Tk；AI 阶段需本地 GGUF 与 `local-ai` 依赖 |
| 本地 Slint 名称排查实验 | `PrivacyFS-GUI.vbs` / `python -m privacyfs.gui` | 仅在本机保留 Slint 源文件时可用；[历史说明](GUI_SLINT.md)。源码安装不再注册 `privacyfs-gui` 命令 |
| 化名清单、上下文审计、镜像、终端浏览 | `privacyfs` | [CLI 手册](user_manual.md) |
| 正文、跨文件关系、任务发布与持久审核 | `inspect` / `relate` / `prepare` / `workspace` | 使用 CLI，详见下方阶段文档 |

Python 的 VBS 启动器使用仓库内 `.venv\Scripts\pythonw.exe`。NTFS 卷读取通常需要管理员权限，可手动运行对应的 `PrivacyFS-Wails-Admin.vbs` 或 `PrivacyFS-Workbench-Admin.vbs`；也可选择普通目录遍历。Wails 的“NTFS 优先”允许回退；工作台默认的“仅 NTFS”模式失败会停止，需手动更换模式。

## 从源码安装

需要 Python **3.10+**。以下 PowerShell 命令在仓库根目录执行，使用项目虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e .
.\.venv\Scripts\privacyfs.exe --help
```

可选能力按需安装：

```powershell
# 仅供保留了本地 Slint 界面源文件的环境使用
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e ".[gui]"

# 本地 GGUF 推理；运行库可能需要本地编译工具
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e ".[local-ai]"

# 开发和测试依赖
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e ".[dev]"
```

Tk 工作台使用 Python 的 `tkinter`，不依赖 Slint；缺少 Tk 的解释器需要先准备相应运行环境。本地模型权重自行准备，不随源码分发，应用不自动下载 GGUF。`requirements-dev.lock` 是在 Windows 环境记录的版本约束快照，不是带哈希的跨平台锁。

HanLP 为另一项可选依赖，首次使用可能下载模型；CLI 的 `--llm` 使用已配置的 Ollama 服务。默认规则检测在本机运行，但配置远程 `llm_url` 会把待复核的原始名称发送到该服务。配置与兼容说明见[用户手册](user_manual.md)；GGUF 推理说明见[本地模型文档](EMBEDDED_AI_LLAMA_CPP.md)。

### 构建 Wails 浏览器

Wails 浏览器独立于 Python 环境。需要 Go **1.25+**、与 `go.mod` 对应的 Wails **v3.0.0-beta.14** CLI，以及 WebView2 Runtime。前端资源直接嵌入，不需要安装 npm 依赖。

```powershell
.\wails-browser\build.ps1
# 已准备并缓存所需模块时，可离线构建
.\wails-browser\build.ps1 -Offline
```

产物为 `wails-browser/bin/PrivacyFS-Browser.exe`，构建后可双击 `PrivacyFS-Wails.vbs`。EXE、资源编译产物和构建缓存不纳入版本管理；`wails-browser/build/` 中的图标、manifest 和构建配置是需要保留的源码资源。更多说明见 [Wails README](wails-browser/README.md)。

## 快速上手

以下命令先使用仓库自带的合成材料。`scan`、`analyze`、`mirror` 和 `tui` 按名称工作，不会自动启用正文分析。

```powershell
# 名称清单：使用明确的合成名单，关闭可选 NER
.\.venv\Scripts\privacyfs.exe scan examples/enterprise-sim-v1/workspace -r --ner off --rules examples/enterprise-sim-v1/controls/rules.synthetic.yaml

# 上下文组合风险审计，不修改被分析目录
.\.venv\Scripts\privacyfs.exe analyze examples/d2 --ner off

# 显式文件正文规则检查
.\.venv\Scripts\privacyfs.exe inspect examples/d2/notes.md

# 显式字段与编号的跨文件关系
.\.venv\Scripts\privacyfs.exe relate examples/d2 --config examples/d2/relations.json

# 安全诊断，不收集用户源文件作为诊断材料
.\.venv\Scripts\privacyfs.exe doctor --self-test
```

对自己的目录使用时，将示例路径替换为实际路径，并把真实规则、私有状态和输出放在仓库外或 `local-data/` 内。镜像必须使用与源目录互不包含的独立目标：

```powershell
privacyfs scan D:\Source -r -f json -o E:\PrivateOutput\listing.json
privacyfs scan D:\Source -r --context-mode enforce
privacyfs mirror D:\Source E:\View --copy
privacyfs tui D:\Source -r
```

上面使用短命令名时，需要激活项目虚拟环境或把它替换为 `.\.venv\Scripts\privacyfs.exe`。普通扫描默认只列一层，`-r` 表示递归；`audit` 报告风险，`enforce` 对已识别风险执行策略并在残留高风险时拒绝输出。

规则示例见 [rules.example.yaml](rules.example.yaml)。统一配置优先级为显式 CLI > YAML > 默认值；YAML 的 NER 关闭值写为 `ner_engine: 'off'`。具体上下文配置语义和全部参数以手册为准。

## 正文与发布工作流

| 阶段 | 当前能力 | 文档 |
|---|---|---|
| D1 `inspect` | 显式 TXT、Markdown、CSV 快照、解析覆盖和规则位置；不自动运行 NER/LLM | [正文检查](DOCUMENTS_D1.md) |
| D2 `relate` | 显式字段/编号关系、来源证据、同名候选和有限风险规则 | [跨文件关系](RELATIONS_D2.md) |
| D3 `prepare → review --approve → export` | 按任务白名单生成新产物，验证实际字节、事实与已知值残留 | [验证发布](RELEASES_D3.md) |
| D4 `workspace` | 本地终端审核、持久纠正、撤销、缓存和修订失效；Windows 状态使用用户 DPAPI | [持久工作区](WORKSPACES_D4.md) |
| D5 文档投影与试用 | DOCX 简单段落/表格、PDF 指定页的文本投影，仅导出新 CSV | [支持矩阵](FORMATS_D5.md) · [试用指南](TRIAL_D5.md) |

这些能力依赖明确的输入、模板与策略。`complete` 只表示所声明范围处理完成，不是隐私认证；部分或失败状态不能当作零风险结果。普通名称化名在同一映射数据库中复用，D3 发布内化名作用域是独立机制。

## 使用边界

- **假名化存在可关联性。** 映射表、目录关系和背景知识都可能帮助恢复身份；未发现风险不等于无法重新识别。
- **镜像不提供沙箱。** 它是普通目录，不能阻止 AI 进程通过绝对路径或其它工具访问原件。
- **硬链接共享内容。** 默认硬链接镜像中写入文件会修改原件；需要内容写隔离时使用 `--copy`。复制不代表正文已脱敏。
- **元数据清理范围有限。** `--scrub-metadata` 针对指定 Office/PDF/JPEG 字段，不处理所有正文、图像内容、附件或文档信息；不支持格式会原样复制并计数。
- **私有界面和报告会显示原始信息。** GUI、工作台、`.pfsreview` 会话及其 JSON/CSV 导出可能包含真实名称和路径，不是可直接公开的脱敏报告。
- **模型仅提供候选。** 本地运行和工具协议校验不证明识别准确率；结果需要人工复核，失败和未检查状态应保留。

退出码通常为 `0` 成功、`2` 参数/配置错误、`3` enforce 拒绝、`4` 处理或 I/O 失败；`inspect` 的部分处理或不支持同样使用 `4`。失败时保留的旧输出不能当成本次新结果。完整契约见[用户手册](user_manual.md)。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest tests -q -rs
.\.venv\Scripts\python.exe evals/run_effectiveness.py --split validation
node --test evals/pi/attack-harness.test.mjs
node --test wails-browser/web/virtual.test.mjs
```

Wails 的 Go 测试在子目录执行：

```powershell
Push-Location wails-browser
try {
    go test -tags production ./...
    go vet ./...
} finally {
    Pop-Location
}
```

默认模型测试使用 mock/stub，不自动启动真实模型、下载权重或发送客户资料。合成回归、真实模型推理、独立效果评测和真人验收是不同证据，不能互相替代。Windows Python 3.10/3.11/3.12 的历史验证见各阶段报告；Wails 当前验证范围为 Windows x64。

[效果与攻击评测](EVALUATION_FRAMEWORK.md) · [NTFS 按需浏览](NTFS_LAZY_BROWSER.md)

## 仓库结构与入库边界

```text
src/privacyfs/       Python CLI、检测、正文、关系、发布和桌面端
wails-browser/       Go / Wails 浏览器及其前端、构建输入
tests/               Python 回归测试
evals/               冻结合成评测集、评测器与 Pi 协议适配
examples/            可分享的合成配置和业务样本
benchmarks/          分阶段合成性能基准
packaging/           离线包构建与安装管理
docs/                文档图片等资源
.review/             保留的诊断脚本；其运行产物被忽略
local-data/          可选本地私有目录，全部忽略，不随源码分发
```

`.gitignore` 排除环境、缓存、构建产物、GGUF、数据库及 SQLite 旁文件、真实规则、私有复核会话和默认命名报告。根 `build/` 是产物目录，`wails-browser/build/` 是构建输入，二者分别处理。合成 YAML 仅按明确路径放行；不整体忽略 JSON、CSV、PDF 或所有 `build/` 目录，以免丢失测试与构建输入。

模型权重不纳入版本管理：已排除 GGUF、GGML、Safetensors、ONNX、CKPT、PT 及常见命名的权重 BIN 文件。根目录的 `models/`、`weights/`、`checkpoints/` 全部忽略；其它命名的 `.bin`、`.pth` 权重放在这些目录或仓库外。可分享的模型配置示例、来源/校验说明和许可证材料放在 `examples/`、`docs/` 等受版本管理的目录中。忽略规则不删除本地模型。

Slint 只按专用文件精确排除，本地文件没有删除。CI 不安装 `gui` extra；混合测试文件中的四项 Slint 界面测试仅在界面源文件缺失时明确跳过，共用后端测试继续执行。`.gitignore` 不控制直接从本机工作目录构建 wheel 的内容；本地仍有 Slint 源文件时，不能据此声称构建包已剔除 Slint。

AI 工作指南、代码评审、阶段实施报告、开发计划和内部审计记录仅本地保存，不纳入版本管理。用户手册、功能使用说明、合成示例、测试、许可证及第三方声明继续保留。

第三方 `pi-main` 参考源码已迁到仓库外，项目运行和默认评测不依赖它；`.gitignore` 仍保留 `/pi-main/` 防止再次误纳入。根目录参考 PDF 保持本地忽略。任意命名的真实输入、映射导出和私有状态应放到仓库外或 `local-data/`；忽略状态标记不等于忽略其整个目录，忽略规则也不是访问控制。首次入库前需核对实际文件清单，不用强制添加绕过规则。

尚未建立 Git 时，可以运行只读检查：

```powershell
.\.venv\Scripts\python.exe .review/check_repository_hygiene.py > .review/hygiene-check.json
```

该检查只核对候选文件和已定义的模式，不是完整隐私认证。建立 Git 后，提交前使用 `git diff --cached --stat` 和 `git diff --cached --name-only` 检查实际暂存清单；`.gitignore` 不会自动移除已经被跟踪的文件，也不会清除已有提交历史。

## 许可证与分发状态

PrivacyFS 自有代码采用 [MIT 许可证](LICENSE)，版权署名为 `FYNIXqwq`。允许按许可证条件使用、修改和分发，包括商业用途；分发副本时需保留版权及许可声明。

第三方依赖和复制的资源继续遵循各自的许可证，不因本项目采用 MIT 而改变授权。本地模型不随源码分发，其来源、版本和再分发条件仍需分别确认。

已包含的 Wails 资源许可及来源、其它依赖的许可注意事项见[第三方声明](THIRD_PARTY_NOTICES.md)。构建和分发二进制包时，还需核对实际打入的组件及其完整许可材料。

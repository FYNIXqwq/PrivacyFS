# 在独立工作进程中加载 llama.cpp

## 当前用法

GUI的“目录 AI（本地模型）”直接加载用户选择的GGUF文件，无需llama-server、端口、HTTP或Ollama。

1. 关闭旧窗口，重新打开`PrivacyFS-GUI.vbs`。
2. 勾选“目录 AI（本地模型）”，点击“选择模型”，选择本地GGUF。
3. 选择要扫描的目录、每批条目数，点击开始。
4. 界面先显示模型加载，再显示目录推理和已检查数量；选中命中项可查看AI理由。取消或关闭窗口会结束工作进程。

本机已在项目`.venv`安装`llama-cpp-python==0.3.35` CPU运行库。新环境需要安装`.[gui,local-ai]`，可能需要本地编译工具；不要把本机wheel当成所有平台通用包。应用不自动下载模型；仅读取明确选择的模型权重，仍不读取被扫描文档的正文。

## 生命周期

`GUI → Windows spawn扫描工作进程 → llama-cpp-python → llama.cpp → 本地GGUF`

GUI进程不导入原生模型绑定、不加载权重。工作进程首次遇到待判断批次时加载模型，同一扫描的后续批次复用它。完成、失败或取消后释放模型并退出；下一轮扫描创建新进程并重新加载，可换用其他模型。

直接本地加载使用llama-cpp-python的[Llama类与聊天接口](https://llama-cpp-python.readthedocs.io/en/latest/)，不启动其server扩展。加载和推理的原生日志在工作进程中抑制，公开错误使用固定错误码，不把异常中的真实路径或提示词写入报告。

分批、相对层级、条目ID校验、UNCERTAIN及未检查计数沿用目录AI流程。中文名称以可读Unicode送入模型，只有JSON控制字符与不能编码的孤立代理码点被转义。ID只是回填键，提示词明确要求不能将它当作隐私线索。

## 资源、取消与失败

- 当前默认CPU推理、4个线程、128K（131072 tokens）上下文；GUI与参数校验均允许2048～131072。未启用GPU层。每批输出上限为32768 tokens；显式选择较小窗口时仍限制为上下文的一半，为输入留出空间。新工作台仍默认每批32条、清单6000 UTF-8字节。扩大模型窗口不会自动扩大分批清单，也不增加跨批聊天历史。权重和KV缓存需要本机内存，128K是否可用取决于模型支持和可用内存。
- `ai_worker_timeout`默认300秒，分别覆盖加载、单批推理和释放操作。GUI监控工作进程；即使原生调用不返回，也能结束超时进程。流式输出在片段之间检查取消/超时，并更新推理进度。
- 用户取消约两秒后仍未结束时强制终止进程；这是生命周期控制，不是操作系统文件/网络沙箱或安全擦除保证。
- 不支持的模型、缺少绑定、坏GGUF、加载失败、超时及异常退出均有明确状态；不会自动退回HTTP，也不会把这些情况记为NONE。
- 模型只选择已枚举的条目ID，不决定路径访问权限或触发文件操作。GGUF应来自用户信任的来源，原生解析器兼容性和安全性不由magic检查证明。

内部`ScanOptions`新增`ai_backend="embedded"`（默认）、`ai_model_path`、`ai_context`、`ai_threads`、`ai_worker_timeout`。目录AI开启且使用embedded时必须指定本地`.gguf`文件，拒绝URL和UNC路径。原服务接口仅在显式`ai_backend="server"`时使用，供兼容调用；GUI已改为本地文件选择。

## 验证记录

默认pytest不加载真实模型。新增合成回归验证：模型只在子进程创建、单轮只加载一次、连续多批复用、正常释放、加载和推理中取消/超时、缺失依赖与坏文件无HTTP回退、GUI传递模型文件、Unicode可读性。

另提供显式真实模型冒烟脚本，只有操作者运行此命令时才加载模型；它只新建并扫描合成目录，拒绝覆盖已有证据目录：

```powershell
.\.venv\Scripts\python.exe benchmarks/embedded_ai_smoke.py --model PATH_TO_MODEL.gguf --output .review/new-embedded-smoke
```

真实模型的加载/调用通过，只能证明该运行库与模型的基本链路可用，不能代替独立隐私精度评测。早期1B模型样例出现误报和漏答，程序如实保留结果及未检查计数，没有过滤掉失败样例或宣称零误报。

嵌入式推理交付时相关回归：56项通过；当时全量回归为**461 passed、3 skipped**，耗时144.45秒。三项跳过为本机无符号链接权限及未安装HanLP的两项测试。保留既有jieba/pkg_resources弃用警告。仅记录Windows Python 3.10.6验证，不借用历史其他版本结果。

128K更新：新增默认值、上限、小窗口兼容、GUI输入范围和子进程传递n_ctx=131072的回归，相关测试47项通过，全量**472 passed、3 skipped**（165.75秒，报告`.review/context-128k-tests.xml`）。现有2B GGUF元数据声明`llama.context_length=131072`，但这不是满窗口推理验证。下方历史真实模型冒烟使用8192上下文，并未验证128K真实推理。已保存的旧会话保留原上下文设置，重新打开后可在配置页改为131072。

输出上限更新：共享上限改为32768 tokens，128K窗口向原生模型和兼容HTTP接口传入32768；8K等较小窗口保留减半限制。相关测试38项通过，全量**473 passed、3 skipped**（162.32秒，报告`.review/output-32768-tests.xml`）。此轮使用mock验证参数及生命周期，未实际生成32768 tokens；256 KiB响应字节保护和操作超时仍然生效。

测试报告保存在`.review/embedded-ai-related.xml`与`.review/embedded-ai-full.xml`；依赖检查和GUI源码编译检查通过。

### 当前运行库与真实模型证据

- Python 3.10.6、Windows x64；llama-cpp-python 0.3.35，使用官方PyPI源码与本机Visual Studio 2022 Build Tools构建CPU wheel，已安装到项目`.venv`，未修改系统Python。`pip check`通过。
- 现成的0.3.19 Windows CPU wheel无法加载所选2B文件，明确报出`unknown pre-tokenizer type: minicpm5`；因此升级到本机编译的0.3.35，而非修改用户模型文件。
- 0.3.35 + `MiniCPM5-2B-Q8_0.gguf`真实运行：4个合成条目、2批、模型加载1次、4条全部取得判断、未检查0、失败批次0；完成后释放模型。用时约65.66秒，CPU 4线程、8192上下文、每批2条。此时间只对应小型合成冒烟，不是全盘性能估算。
- 该轮只有1个命中：`medical/张三-13812345678.txt`被标为CONTACT，理由是含姓名和手机号；`software/LICENSE-MIT`以及两个泛称目录没有被标记。完整私有合成证据位于`.review/embedded-ai-smoke-2b-resume-20260918/report.json`；原件是新建的合成资料，没有扫描用户真实目录或正文。
- 早期1B结果出现将ID/普通目录当作隐私以及漏答等问题，保留在`.review/embedded-ai-smoke-*`中。4条样例并非独立精度评测，不能由此推导对其他目录的误报率。

本机构建产物的原位置：`.review/local-ai-build/llama_cpp_python-0.3.35-py3-none-win_amd64.whl`；SHA-256：`ded1ba8663c086aa23e377d8fb6495b0233b255a5b4402610d5530ad669f5c74`。2026-09-19隐私清理时，原wheel及带有编译路径的本机原生库已迁到仓库外；wheel哈希未变，当前环境通过目录连接加载原生库，这些本机运行产物不随源码分发。它没有包含GGUF模型，也不是已签名发布包。构建用到以下过程级设置，未持久更改用户环境：

```powershell
$env:CMAKE_GENERATOR = 'Visual Studio 17 2022'
$env:CMAKE_GENERATOR_PLATFORM = 'x64'
$env:CMAKE_BUILD_PARALLEL_LEVEL = '4'
$env:CMAKE_ARGS = '-DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF -DGGML_OPENMP=OFF -DLLAMA_CURL=OFF'
.\.venv\Scripts\python.exe -m pip wheel --no-deps --no-binary=llama-cpp-python llama-cpp-python==0.3.35 --wheel-dir .review/local-ai-build
```

这是复现本机构建的说明，不要求普通用户每次扫描都编译；当前项目环境已准备好。

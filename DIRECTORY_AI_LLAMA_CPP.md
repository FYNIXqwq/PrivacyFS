# 目录上下文 AI 检查（llama.cpp）

**后续更新：当前GUI已改为独立工作进程直接加载GGUF，不需要server。请使用[本地进程说明](EMBEDDED_AI_LLAMA_CPP.md)。本文的端口/别名及旧GUI操作是此前服务版的历史说明，当前不再提供这些GUI控件；服务传输实现仅供显式`ScanOptions(ai_backend="server", ...)`兼容调用。下方448项测试与HTTP stub记录保留原时点含义。**

2026-09-18新增的GUI可选模式。它把一批文件/目录名称及其相对层级交给本机llama-server，由模型标记疑似隐私条目；不读取正文、不自动重命名或删除文件。

## 使用

1. 准备已安装的llama.cpp与本地GGUF模型，启动llama-server。下面的程序和模型路径需替换成实际路径：

```powershell
& 'C:\Tools\llama.cpp\llama-server.exe' -m 'C:\Models\model.gguf' --alias privacyfs-local --host 127.0.0.1 --port 8080 -c 8192 --jinja
```

模型需支持当前llama.cpp版本及聊天模板；这里没有自动安装、模型下载或精度保证。启动参数与模型别名机制见[llama.cpp服务器文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。

2. 关闭旧GUI，再双击仓库`PrivacyFS-GUI.vbs`。
3. 勾选“目录 AI（llama.cpp）”，填写端口（默认8080）和模型别名（默认privacyfs-local），选择每批最多32、64或128条（默认64）。旧Ollama开关需关闭，两种模式互斥。
4. 选择目录后开始扫描。结果详情显示“目录上下文”、AI标签和原因；UNCERTAIN显示为“AI 待复核”。查看“已返回、已发送、未检查、不完整批次、超长条目”计数，不能只看命中数量。

这一模式直接展示模型判断，不使用旧名称检测器、jieba或父目录命中传播；YAML只用于加载排除配置，不把名单/关键词当作模型结论。需要明确规则的检测时，使用普通规则模式。现有CLI和旧Ollama名称补充未改为llama.cpp；新模式不通过Ollama转发。

## 怎样分批

- 只枚举当前扫描范围内可访问且未排除的条目。小目录树在条目数和长度预算内时，一批发送完整清单，包括不同层级。
- 每条包含本轮独立ID、相对路径、文件或目录类型。同名条目不去重，例如software/record.txt和medical/record.txt可有不同判断。
- 清单超过预算时，优先在父目录变化处分批，尽量把当前同级组保留在下一批；单个目录过大时继续拆分。相对路径在每个分段中保留祖先关系。
- 同时限制每批条目数与序列化清单长度：默认64条、6,000个UTF-8字节。JSON转义也计入预算。单条过长时不截断，不发送，计入ai_oversized和未检查。
- 清单按遍历顺序流式组装，不把全盘清单留在内存。不保留跨批模型会话或生成摘要；每批独立判断。跨批组合线索可能遗漏，不能声称模型同时理解了整个C盘。
- 没有“最多500个名称”的总预算，正常响应时会继续遍历和分批。两批连续不完整或失败后停止本轮AI请求，继续遍历统计覆盖缺口，最终为partial。

6,000字节是清单长度限制，不是模型token精确测量；请求还包含固定提示词和输出格式约束，输出最多4,096 tokens。示例服务上下文为8192，需要按模型/显存配置。若服务上下文不足或输出被截断，该批不会被当成成功；可减少每批数量后重扫。内部ScanOptions另支持ai_input_bytes=512..32768及ai_timeout=1..300秒，GUI当前不暴露这两项。

## 请求与返回

请求使用直接HTTP连接`127.0.0.1:<端口>/v1/chat/completions`，不采用环境代理、不跟随重定向、不读取凭据、不调用下载/模型管理端点、不提供远程URL。输入是system提示与独立user JSON；文件名中的指令、网址或伪造角色只是数据。传输使用llama.cpp的[聊天与结构化JSON接口](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1chatcompletions-openai-compatible-chat-completions-api)。

以下是合成输入条目的形式，实际由程序自动生成：

```json
{"id":"E17","path":"medical/record.txt","kind":"file"}
```

模型需为每个ID返回label和reason：PERSON、CONTACT、ORG、IDENTITY、ADULT、UNCERTAIN或NONE。程序只接受本批存在的ID，拒绝重复/未知ID、额外字段、未知标签、重复JSON键、超长原因、工具调用与非正常结束的生成；单次响应最多256 KiB。不会使用模型生成的路径进行任何文件操作。

缺失ID计入未检查，不填充NONE。NONE仅表示模型未发现线索；UNCERTAIN表示模型提出疑点，需要人工复核。complete表示已访问条目均取得可解析判断，不是隐私认证；privacy_verified始终为false。模型仍可能幻觉、误报、漏报或错误地给出NONE。

请求只发送条目相对路径与类型，不发送绝对根路径、文件正文或规则文件内容。选择的根目录自身不作为条目检查。相对路径可能包含真实人名和组织，接收方是用户配置的本机llama-server；该服务自身的日志、模型和运行配置不由PrivacyFS管理。

## 状态、取消与导出

- GUI仍使用可终止的独立扫描进程和有界消息队列；结果使用已有Windows DPAPI临时存储、分页与流式JSON导出。
- 默认HTTP socket超时60秒，不等同于整个模型任务的硬时间界限。取消约两秒后会强制结束未响应的客户端worker；不保证服务器已停止生成。
- JSON新增directory_ai_enabled与settings中的ai_backend、端口、模型别名、批次预算。stats新增ai_batches、ai_attempted、ai_checked、ai_unchecked、ai_failed_batches、ai_oversized、ai_uncertain、ai_stopped。
- AI命中包含entry_id、attribution=context、source=DirectoryAI及reason/label；未检查条目只统计数量，没有被列成“疑似隐私命中”。取消/错误的报告不能当作完整扫描。
- 模型原因和真实路径只作为本地私有报告展示；导出包括全部已保留命中及覆盖状态，不限当前页。新模式没有跨扫描判定缓存，不写mapping.db。

## 验证记录

新增tests/test_directory_ai.py，先运行缺失实现时失败，再实现功能。相关测试（目录AI、GUI后端、Slint主题）43项通过。

覆盖小树整批、530条完整分批、目录边界、重复文件名独立ID、超长条目、漏答、连续失败停止、拒绝伪造ID和标签、协议输入数据边界、重定向/响应溢出/截断拒绝、配置校验、真实Slint选项及原因展示。使用真正本机HTTP stub验证请求路径与序列化，不读取或发送正文；另通过Windows spawn验证在HTTP请求进行中取消worker、保留未检查状态。

最终全量测试：**448 passed、3 skipped**，耗时317.19秒，使用已有Windows Python 3.10.6环境。跳过项为本机无符号链接创建权限，以及未安装HanLP的两项测试；有既有jieba/pkg_resources弃用警告。GUI源码编译检查通过。

未在命令路径或本次仓库检索中找到可用llama-server；没有安装或启动真实模型，没有发送用户文件名，没有做真实模型精度评测。上述stub通过只证明调用与数据处理流程，不证明某个GGUF模型能准确识别隐私或遵循所有判断规则。

**D1 文档检查接口与边界**

D1在现有文件名工具旁增加一个显式内容入口，支持本地TXT、Markdown和CSV。它提供快照、解析覆盖、来源定位和规则基线，不提供正文安全发布、跨文件推断或Agent访问隔离。

**命令行**

```powershell
privacyfs inspect C:\Examples\notes.txt C:\Examples\table.csv --rules C:\Config\rules.yaml
privacyfs inspect C:\Examples\legacy.csv --encoding gb18030
privacyfs inspect C:\Examples\notes.txt --max-file-bytes 1048576 --max-seconds 5
```

路径仅为使用示例。inspect要求显式列出文件，不递归扫描目录；同次调用的文件需共享可用的文件系统根。scan/analyze/mirror/tui保留原有文件名行为，不自动读取正文。

输出为JSON，使用document_id、revision_id和input_index对应本次输入。没有真实路径、敏感表面或内部内容摘要。字段含义：

| 字段 | 含义 |
|---|---|
| status | complete、partial、unsupported或failed；是本次处理状态 |
| parsing_status | 原始解析状态；检测超时可使status失败而解析状态仍完整 |
| reason | 固定错误/限制代码，不回显原始异常正文 |
| scope | literal_file_text；路径适配器则为filename_only |
| parsed_characters / total_characters | 解码后的字符覆盖计数；失败可能不知道总字符数 |
| finding_count | 只有解析和检测完整时才是整数；失败/部分处理为null |
| findings | 类别及来源位置；不含原文 |
| privacy_verified | 始终为false；完整解析或零命中不能证明隐私安全 |

退出码0表示所有指定文件完成声明范围内的检查，**不表示没有敏感信息**；2表示参数/规则配置错误；4表示读取、解析、限额、检测或输入范围失败/不完整。发生根目录或配置错误时可能只有stderr，未形成JSON报告。中断或输出管道失败也可能留下不完整JSON，应检查退出状态。

inspect读取明确名单、机构名单、关键词、正则、职业和身份词配置，采用`d1-rules-v1`。不会启用HanLP、jieba、Ollama、拼音模型或现有按文件名设计的NER，无自动联网，也不使用mapping.db。它不保证发现没有被名单或规则覆盖的人物/机构。

**编码与格式**

- 默认识别UTF-8/16/32 BOM，无BOM时严格UTF-8；不猜测本地系统编码。
- 可显式指定`utf-8`、`utf-16-le`、`utf-16-be`、`utf-32-le`、`utf-32-be`或`gb18030`；与BOM冲突时失败。
- BOM不进入解码正文；原始CRLF/LF/CR保持。位置单位是Unicode码点，不是字节或UTF-16代码单元。
- TXT与Markdown均作为字面文本处理，Markdown代码、链接和图片地址不会执行或访问。未读取的外部资源不属于literal_file_text范围。
- CSV采用逗号分隔、双引号引用和双写引号转义；不自动猜分隔符或删除空白。保留空单元格、重复表头、不同长度记录、空记录、多行单元格、前导零和公式字符串，不执行公式。
- 首行保留为普通记录，行列位置可用于未来表头建模；D1不跨行或跨列拼接姓名，不推断字段类型与外键关系。
- 引号未闭合、闭合引号后出现非法字符等返回INVALID_CSV。超过CSV行/列/单元格限额时，只保留完整已解析记录，返回partial及未完成原因。
- PDF、Office和OCR正文不在D1范围；此前mirror的元数据清理仍是独立功能。

**来源模型与库接口**

```python
from pathlib import Path
from privacyfs.config import Rules
from privacyfs.documents import DocumentSession, ParseStatus, inspect_document, public_report

with DocumentSession(Path("C:/Examples")) as session:
    snapshot = session.capture("notes.txt")
    document = session.parse(snapshot)
    if document.coverage.status is ParseStatus.COMPLETE:
        findings = inspect_document(document, Rules(literal_names=["张三"]))
        safe_json_data = public_report(document, findings)
        # document.resolve(finding.locator) 是仅供可信本地调用方使用的原文接口。
    session.release(snapshot)
```

真实名单应放在私有规则文件，示例中的姓名仅为合成占位。低层API仍要处理ProcessingStopped、ClosedSessionError和StaleLocatorError；不能捕获异常后把它转换成“零发现”。

DocumentSnapshot以会话内键控摘要标识私有内容，公开ID为不透明值。相同会话、相同规范化路径复用document_id；每次capture产生新revision_id。parse只读取已捕获字节，不重新打开源文件。文件修改后，旧快照仍可回放，但旧locator不能应用到新revision。

SourceLocator的start/end是解码原文的左闭右开区间，line是从1开始的物理行；CSV row/column从0开始，包含首行。block_start/end则是解码单元格或内容块的位置。CSV中一个逻辑引号对应原文的两个转义引号，resolve可能返回序列化片段而不是单元格解码值。

`session.adapt_path_entry(detected_entry)`将已有路径信号转换成同一定位模型，不打开源文件正文。普通命令尚未改用正文工作流。

**规范化与替换**

规范化策略是NFKC、casefold和字形簇内的OpenCC t2s。简繁转换主要面向常用汉字，采用局部转换，不是词组翻译，也不能将规范化相同直接当作同一人物。报告记录Unicode数据库、regex与OpenCC版本。

膨胀、组合或重排可能使多个规范化字符对应一个原文字形簇。此时返回覆盖原文的范围；如果只命中规范化单元的一部分，会标记covering。ASCII/常用汉字等可安全定位的连续字符用紧凑线性区间，避免逐字符对象造成内存和CPU开销。

`source_map.apply_edits()`统一按原文左侧优先、同起点最长、相同区间保留输入优先级处理重叠；替换结果不再被二次匹配。它只是标量文本编辑原语。CSV或复杂文档必须经过结构感知的重新序列化与验证，D1不提供可直接对外发布的安全副本。

**资源和生命周期**

| 默认限额 | 数值 |
|---|---|
| 单文件捕获 | 8 MiB |
| 同时保留的快照字节 | 64 MiB |
| 每会话capture尝试 | 1,000次 |
| 单文档解码/规范化字符 | 4,000,000 |
| 会话托管文本计量 | 16,000,000字符，保守累计正文、块值和规范化视图 |
| 块数 / CSV行数 | 100,000 |
| CSV列数 / 单元格长度 | 1,024 / 65,536字符 |
| 单文档发现数 | 10,000 |
| 处理时限 | 每个捕获/解析/检测阶段10秒 |

max_total_bytes是内存驻留限额，不是累计披露预算；release后可供后续文件使用。CLI逐文档写JSON和释放内存，不累计保存全部原文与发现对象。危险自定义正则使用可超时引擎；其他步骤在处理检查点响应取消/超时，不能强制中断单次阻塞的操作系统I/O。

会话不主动创建磁盘明文快照。close清除托管字节和IR内容并使文档/块句柄失效；不能保证调用者已保存的字符串、finding副本、操作系统换页或崩溃转储被安全擦除。会话按单线程使用设计，取消信号可由其他线程设置；不承诺并发共享同一会话。内部模型的repr隐藏敏感字段，但直接asdict仍可能暴露内部数据，必须使用public_report。

捕获会拒绝重解析点、越界路径和非普通文件，并检查读取前后的身份/元数据以发现常见变化。源目录应保持静止；这不是对同权限恶意并发写者的原子文件系统快照或沙箱保证。

**评测与性能**

冻结集、分组和指标说明见 `evals/README.md`。运行 `python evals/run_d1.py --split validation`；它测试给定名单/规则下的定位与结构，不代表真实NER、跨文件推理或任务效用。

`python benchmarks/documents_d1.py`生成并清理合成文本，测捕获、解析和默认规则检查。它不运行模型、跨文件图或导出；报告的RSS是进程生命周期峰值。可通过上述命令在当前环境复验。

# D5 文档支持矩阵与选区契约

首个工作流：DOCX客户表 + CSV会议索引 + PDF选定页的合作状态记录，导出新的去身份化CSV。版本0.2.0a1为可试用版本，不是任意Office/PDF完整匿名化工具。

| 输入 | 检查/定位 | 关联和发布 | 未覆盖内容 |
|---|---|---|---|
| TXT/Markdown | D1原始解码文本、字符位置 | 原D2字面记录语法，原D3输出契约 | 任意自然语言推断 |
| CSV | 原行/列、引号、多行单元格 | 原字段约束，CSV重建 | 公式执行、宏 |
| DOCX段落 | 主文档直接段落中的文本、tab、换行和已支持字符；part/paragraph位置 | source_view=docx_paragraphs；内容必须符合字面记录语法；输出CSV | 原排版、Word页码、页眉页脚、批注/脚注/尾注、附件和图片等 |
| DOCX表格 | 主文档直接表格的简单矩形单元格；table/row/column/part | source_view=docx_table，必须指定table_index；按表头映射；输出CSV | 合并/嵌套表格、复杂控件、未处理主文本结构 |
| 文本型PDF | pypdf静态页文本；page和抽取文本字符位置 | source_view=pdf_pages，必须显式指定递增pages；每行符合记录语法；输出CSV | OCR、布局/阅读顺序证明、表格自动识别、动态表单、附件、注释及元数据 |
| 加密或全无文本PDF | 明确UNSUPPORTED/原因 | 拒绝 | 不尝试密码或OCR |
| DOCM/XLSX/扫描件 | 不在本次范围 | 拒绝或保留未支持状态 | 不复制原件作为回退 |

DOCX/PDF的外层解析状态始终是PARTIAL，原因CONTAINER_PROJECTION_ONLY；处理完所选文本不代表原文件全部处理。`unprocessed`报告固定类别，`projection_complete`只表示支持的文本抽取可用于显式选区。`inspect`的finding_count仍为null并以非零状态结束，不把部分格式伪装成全文件安全。

关系配置示例：

```json
{
  "schema_version": "d2-templates-1",
  "files": [
    {
      "path": "clients.docx",
      "source_view": "docx_table",
      "table_index": 0,
      "primary": "cid",
      "fields": [
        {"name": "cid", "role": "key", "namespace": "customers", "entity_type": "client"},
        {"name": "name", "role": "identity"}
      ]
    },
    {
      "path": "notes.pdf",
      "source_view": "pdf_pages",
      "pages": [1],
      "primary": "mid",
      "fields": [
        {"name": "mid", "role": "key", "namespace": "meetings", "entity_type": "event"},
        {"name": "status", "role": "sensitive", "values": ["paused", "ended"]}
      ]
    }
  ]
}
```

DOCX table_index、paragraph、行列均从0开始；PDF page从1开始。pages必须非空、唯一、递增；无文本的选定页会拒绝，不自动跳过。其他未选择页/表格/段落不会作为附件或隐藏文本带出。没有source_view的复杂格式不会进入D2/D3正文关联与发布。

DOCX/PDF定位单位为`extracted_unicode_codepoint`，相对于当前快照的拼接抽取文本视图；它不是ZIP/PDF原始字节偏移，也不是可直接编辑的版面坐标。私有locators保留part、page、paragraph、table及行列；公开检查报告仅输出必要的安全ID/数值位置。缓存保存并验证这些位置，解释版本变化使旧定位/纠正失效。

主文本中的修订、域、隐藏文本/隐藏样式、符号字形、数学结构、文本框、控件等可能使projection_complete=false，禁止发布；不会通过删除这些节点后拼接出一个看似合法的新编号。标准连字符和软连字符按声明字符保留。对复杂主文本采取保守拒绝，不能把任意Word文件认作可发布。

页眉、图片等未处理内容只记录类别；不抓取外链、不打开嵌入对象、不执行宏/域/JavaScript，不把原DOCX/PDF复制到输出目录。原字节仍用于快照/版本核验。已选文本中，仅TaskProfile批准的字段和值进入新CSV；D3继续核对事实、别名关系、禁止项、产物字节、额外文件和NTFS命名流。

解析在独立可终止进程中进行。Windows使用直接基础解释器和Job Object，在发送文件字节前设置资源限制；避免venv启动器的子进程绕过限制。默认每文件8 MiB、ZIP展开/累计PDF流32 MiB、ZIP条目1000、PDF页100、文本400万字符，既有单元格/块限制继续生效；子进程提交内存默认512 MiB，并限制为单进程。主进程监控超时/取消并结束子进程。它是资源边界，不是D6文件/网络访问沙箱。

PDF抽取文本可能不同于视觉阅读顺序，OCR层也可能有识别错误；pypdf不执行OCR。当前不能证明页面显示、隐藏文字或字体映射与人眼理解完全一致。操作者应核对所选页和事实；诊断/合成通过不代表客户文档普遍准确。真正业务试用按用户安排延期。

工作区缓存、来源模型和发布验证版本已更新。旧数据仍可读取，缓存会重建；依赖旧解释版本的纠正可能需复核，旧未发布计划不能静默用于新解释。已发布文件及其历史不会被改写。

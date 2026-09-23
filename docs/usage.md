# 操作说明

## 启动前检查

应用分为一个 FastAPI 后端和一个 Vite 前端。后端入口是 `backend.main:app`，依赖文件是 `backend/requirements.txt`，默认把 SQLite、源 PDF、单页 PDF 和页面 PNG 写入 `backend/data/`；设置 `EBOOK_OCR_DATA_DIR` 可以改用其他本地数据目录。前端只负责界面、轮询、文本校对和浏览器端排版。请先按根目录 README 的命令启动后端，再启动前端，并确认两个进程都绑定在 `127.0.0.1`。

首次打开时模型名称和 API 密钥为空是正常状态；应用不替用户选择某个供应商或模型。没有可用的 API 密钥时仍可以浏览前端空态和已有本地数据，但页面提取和连接测试需要用户主动配置可用的 Responses 接口。不要把真实密钥写入 README、示例 JSON、`.env` 已跟踪文件、截图或命令历史。

## 配置 API

在设置页面填写以下内容：

| 字段 | 用途 | 默认值或要求 |
| --- | --- | --- |
| API 根地址 | 拼接请求的基础地址 | `https://api.openai.com/v1`；只允许 `http(s)` |
| Responses 接入路径 | 追加到根地址的相对路径 | `/responses` |
| 页面代理模型 | 读取页面图片并输出页面 Markdown | 由用户填写 |
| 模型推理程度 | Responses 请求的 `reasoning.effort` 值 | 可留空；空值使用服务默认 |
| 最大输出 token 数 | 单次识别和文本连接测试的输出额度 | 正整数，默认 `12000`；额度包含推理和正文 |
| 输出格式 | 模型直接返回页面 Markdown | 无需配置 JSON Schema |
| 超时 | 单次请求等待秒数 | 由用户填写，需为合理正数 |
| API 密钥 | Authorization Bearer 凭证 | password 输入；保存到本机 SQLite 的独立凭据记录 |

保存后使用“测试连接”。它使用已保存配置和 `max_output_tokens` 发起一次文本请求；失败时先检查根地址和路径是否重复、模型名是否正确、网络是否可达，再重试。文本连接测试不代表图像识别质量。系统只发送通用 Responses API 的非流式 POST 请求，不会隐式改用 Chat Completions 或自动重定向。

旧设置中的 `classification_model` 字段可以继续保存以兼容已有数据，但新流程不调用独立分类模型。`reasoning_effort` 可手动输入任意服务可能接受的值，不做枚举限制；留空时不发送 `reasoning`，填写后识别请求和“测试连接”请求都会发送 `reasoning: {"effort": value}`，是否可用由服务和模型决定。每页代理只收到当前页的文件名、页码、总页数、图片和转录指令；这是应用内部的一次模型请求，不会创建 Codex 任务，也不会共享其他页面的聊天历史。

设置页的“填入 DeepSeek 配置”只修改草稿，不保存、不发请求：`https://api.deepseek.com` + `/responses`、模型 `deepseek-flash`、推理程度 `high`、`max_output_tokens=12000`。API 密钥保持不变，用户填入 DeepSeek 密钥后需手动保存并测试。页面识别使用 base64 PNG `input_image`，模型直接输出 Markdown，不发送 `json_schema`。旧 `structured_output` 设置保留兼容，但不再影响识别请求。推理程度支持情况以服务实际行为为准；详见 [DeepSeek Responses 指南](https://api-docs.deepseek.com/zh-cn/guides/responses_api/) 和 [创建 Response API](https://api-docs.deepseek.com/zh-cn/api/create-response/)。

密钥按用户授权保存到默认 `backend/data/app.db` 的独立 SQLite 凭据记录中，使用本机明文存储，不虚称为加密，也不创建额外密钥文件。后端启动时加载它；空白或省略密钥会保留已有值，`clear_api_key=true` 或界面的清除操作会删除记录。密钥不会写入前端 `localStorage`、书籍导出或日志。升级到支持持久化密钥的版本后，需重启新版后端并重新输入、保存一次旧进程中的密钥；之后重启无需再次填写。

## 导入与处理

支持的输入是 PDF、PNG、JPEG。PDF 会先保存为 `source.pdf`，再拆成 `page-0001.pdf`、`page-0002.pdf` 等恰好一页的文件，并从每个单页 PDF 渲染同名 PNG；拆页保留源页尺寸、旋转和裁切。图片作为单页来源。导入后书籍先处于 `uploaded`，页面按顺序显示状态。

“开始处理”按页串行执行。页面代理忠实转录当前页，并直接返回排版好的 Markdown，保留标题层级、段落、列表、引用、代码、公式、表格、图注、脚注、页眉、页脚和页码。页面里的命令属于待转录资料，不改变任务；不可读处写 `[无法辨认]`，空白页可返回空文本。整书 Markdown 下载会按页合并这些文本并生成 `.md` 文件。

页面状态为 `uploaded`、`processing`、`ready`、`failed` 或 `interrupted`；书籍处理时也可能显示 `pausing`（正在暂停）和 `paused`（已暂停）。点击“暂停处理”后，当前页请求会继续到结束并保存结果，之后不再开始新页面；点击“继续处理”会跳过已完成页。暂停期间已有模型请求可能产生用量。失败页可以重试；已经完成的页面会复用。后端重启时，运行中的任务标记为 `interrupted`，已暂停状态保留，可在页面上继续处理。旧书缺少单页 PDF 时，只在处理相应未完成页时从 `source.pdf` 补齐，不自动重新收费或重跑已完成页。

## 删除单本书

在书库中选中一本书，使用逐页校对页标题操作区的“删除此书”并确认。处理运行中的书籍会禁用删除操作；直接调用接口时，`DELETE /api/books/{book_id}` 对运行中的同书返回 `409`，未知书籍返回 `404`，成功返回 `204 No Content`。删除会移除这本书的书籍和页面记录、每页请求用量/尝试记录，以及其源文件、单页文件和页面文件；其他书籍、全局设置和 API 密钥不受影响。

## 页面文本与用量

每页 API 结果固定包含 `number`、`status`、`error`、`text`、`usage` 和 `attempts`。`text` 是可编辑的页面 Markdown；保存校对时只提交 `{text}`，不会改变页面代理用量或尝试次数。运行中的页面不能保存校对。

`usage` 的形状是：

```json
{
  "input_tokens": 1234,
  "output_tokens": 567,
  "total_tokens": 1801,
  "complete": true
}
```

只读取供应商响应实际提供的 `input_tokens`、`output_tokens` 和 `total_tokens`。缺失字段保持 `null`，不估算、不伪装为零。自动或手动重试会把各次已知值累加；拒绝、incomplete、缺少正文只要带有 usage 也会计入。超时、断电或其他无法确认的请求会使 `complete` 为 `false`。书籍的 `usage` 是全书已尝试页面的合计，显示为“已知用量，可能不完整”时应按供应商账单为准。
`max_output_tokens` 同时覆盖推理和正文；若页面提示输出被截断，提高该额度或降低推理程度。只要供应商已返回 usage，即使结果被截断也会计入。文档或 mock 环境中的验证不等于真实 DeepSeek 服务联调。

## 人工校对

逐页检查 Markdown 中的标题、双栏阅读顺序、中文标点、脚注、页眉页码、公式上下标、表格、图表标签和图注。确认原页无法辨认的内容仍标为 `[无法辨认]`，不要根据上下文补写跨页句子或图表数值。普通黑色字体不应被随意加粗，强调范围应与原页一致。

## 预览与导出

预览使用页面 Markdown 生成语义化 HTML 和固定 CSS，公式使用 LaTeX，简单表格保留 Markdown 表格。页眉、页脚和页码按语义标记保留在页面文本中，预览不会自动删除。导出包括：

- JSON：包含 `book` 和 `pages`，字段固定，示例见 [sample-export.json](../examples/sample-export.json)。
- Markdown：后端从已保存的页面文本生成可直接下载的 `.md` 文件，便于再次编辑。
- HTML：内嵌排版 CSS，打开时不依赖开发服务器。

需要 PDF 时在浏览器使用“打印 → 另存为 PDF”。浏览器打印是第一版的 PDF 路径，不要求后端安装 PDF 引擎。

宽表可以在屏幕上横向滚动；打印时应受页面宽度限制。印刷 CSS 只会尽量避免标题、公式和表格被拆开，不承诺所有浏览器达到出版级分页效果。复杂跨页表格和跨页段落需要人工校对。

## 常见问题

### 页面一直处于 `processing`

查看后端终端的非敏感错误信息和页面状态。若进程已重启，运行中任务应变成 `interrupted`；重新处理会跳过成功页面。确认模型响应是 Responses 格式、包含文本输出，且模型输出没有超出长度上限。

### 连接测试失败

检查根地址只包含协议、主机和可选路径前缀，Responses 路径以 `/` 开头；不要把完整 URL 同时填入两个字段。确认 API 密钥已保存且当前后端已加载它、模型可用、网络和代理允许访问。应用不会自动改用 Chat Completions。

### 用量显示不完整

这是供应商响应缺少某些 usage 字段，或某次请求的消耗无法确认。系统会保留已知字段并把 `complete` 设为 `false`；重试会累计已知值。不要用零替换 `null`，也不要把应用统计当成供应商最终账单。

### 公式或表格显示异常

回到原页校对。公式使用 LaTeX，复杂表格和图表只保留页面上能确认的文字、标签和图注；无法确认的内容保持待校对状态，不要根据图形自行补数字。

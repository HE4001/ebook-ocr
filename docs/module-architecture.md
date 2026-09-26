# 架构模块说明

项目按“本地前后端分离、逐页处理、文本校对和导出”的边界组织。根目录 [ARCHITECTURE.md](../ARCHITECTURE.md) 保留既有架构记录；本文维护当前处理范围、状态和 HTTP 接口要求，并解释运行时职责。

## 模块与责任

| 模块 | 责任 | 关键边界 |
| --- | --- | --- |
| `frontend/` | React + TypeScript + Vite 界面；书库、导入、轮询、页面文本校对、预览、导出和单本书删除 | 不保存 API 密钥到浏览器；不绕过 `/api` 修改后端数据 |
| HTTP API | FastAPI 路由、请求校验、状态和错误文本 | 所有接口使用 `/api` 前缀；ID、页码和路径必须验证 |
| 导入与页面文件 | 验证 PDF/PNG/JPEG；PDF 保留 `source.pdf`，拆成单页 PDF，再从单页 PDF 渲染 PNG | EPUB/MOBI 暂不处理；文件、页数和像素有上限 |
| 持久化 | SQLite 保存设置、独立凭据、书籍、页面文本、用量和尝试次数；文件系统保存源 PDF、单页 PDF 和 PNG | 默认目录为 `backend/data/`，可用 `EBOOK_OCR_DATA_DIR` 覆盖；凭据记录在 SQLite 中本机明文保存，其他密钥文件不落盘；删除书籍时一并删除其记录和文件 |
| Responses 客户端 | 组合 `base_url + responses_path`，发起带 Bearer 的非流式 POST 并读取供应商 usage | 只支持通用 Responses API；不自动切换 Chat Completions、不开 provider 框架或跟随重定向 |
| 页面代理 | 每次请求只处理一页图片，输出结构化页眉、正文 Markdown 和页脚 | 每页独立上下文；应用内模型请求不创建 Codex 任务，不携带其他页聊天历史 |
| 流程协调 | 按当前页、手动页码列表或整本范围串行处理；累计尝试和已知用量 | 可重复处理已完成页；成功替换旧文本，失败保留旧文本；未知请求不会伪造为零用量 |
| 排版与导出 | 前端用同一套页面结构与模型输出的页眉页脚格式生成单页预览、整书预览和独立 HTML；另提供 Markdown 与固定字段 JSON | 页眉置顶、页脚置底、正文自然伸展；不精确复刻源版面或承诺每个源页对应一张打印纸；PDF 使用浏览器打印 |

## 数据流

```text
PDF / PNG / JPEG
       │
       ▼
验证与页面文件 ──► source.pdf、单页 PDF、页面 PNG、Page
       │
       ▼
独立页面代理 ───► Page.text + Usage + attempts
       │
       ▼
人工校对 ───────► PUT /api/books/{id}/pages/{number}
       │
       ├─────────► Markdown / JSON
       └─────────► 内嵌 CSS 的 HTML ─► 浏览器打印 PDF
```

页面代理的单次上下文包含文件名、页码、总页数、本页图片和转录指令。页面中的命令都属于待转录资料，不改变任务。代理只转录原书排印内容，排除后加手写批注等类似笔迹；按原页阅读顺序把页眉、正文、页脚分开，脚注和图注留在正文。不可读的原文局部写 `[无法辨认]`，空白页返回空语段和空正文。

模型返回 `header_segments`、`body_markdown`、`footer_segments`，后端按严格 JSON Schema 请求并校验。每条页眉或页脚语段包含必填的 `kind`、`text`、`alignment`、`row`、`font_size`、`bold`、`italic`；对齐为 `left|center|right`，行次为 1–10，字号为 `small|normal`，粗体和斜体为布尔值。旧保存结果缺少格式字段时使用居中、第一行、小号字、非粗体、非斜体的默认值。只有正文使用 Markdown；正文中的公式行内使用 `$...$`，独立公式的 `$$` 分隔符各自独占一行，中间保留 LaTeX。页眉页脚与正文分别保存。

`Page` 固定包含 `number`、`status`、`error`、`text`、`header_segments`、`footer_segments`、`usage` 和 `attempts`。`Usage` 为 `{input_tokens, output_tokens, total_tokens, complete}`：供应商没有返回的字段保持 `null`，不会估算或显示为零；一次或多次重试只累加已知值，任意尝试缺少用量或无法确认消耗时 `complete` 为 `false`。`Book.usage` 是全书已尝试页面的合计。

## HTTP 契约索引

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 返回 `{"status":"ok"}` |
| GET / PUT | `/api/settings` | 读取或保存非密钥设置；空密钥表示保留，`clear_api_key=true` 清除 |
| POST | `/api/settings/test` | 使用已保存设置主动测试连接 |
| GET | `/api/books` | 书库摘要 |
| POST | `/api/books` | multipart 上传 PDF/PNG/JPEG |
| GET | `/api/books/{id}` | 书籍和页面详情 |
| POST | `/api/books/{id}/process` | 可选 `{"pages":[1,3]}` 处理所选页；省略请求体或 `pages` 处理整本（含已完成页）；页码列表需非空且为合法页码，去重并排序；同书运行中返回 409 |
| POST | `/api/books/{id}/pause` | 请求暂停；当前页结束后停止后续页面，未运行时返回 409 |
| DELETE | `/api/books/{id}` | 删除单本书及其页面、请求用量记录和文件；运行中返回 409 |
| PUT | `/api/books/{id}/pages/{number}` | 保存人工校对后的 `{text}` |
| GET | `/api/books/{id}/export` | 下载 `{book,pages}` 结构化 JSON |
| GET | `/api/books/{id}/export.md` | 下载按源页顺序合并的 Markdown 文件 |
| GET | `/api/books/{id}/assets/{name}` | 安全范围内读取已保存页面文件 |

所有响应字段都应保留契约约定的 nullable 字段。错误使用 FastAPI 的 `detail` 文本。前端通过轮询观察状态，不引入 WebSocket。

默认范围是当前页，用户也可手动选择多页或整本。页面可以重复处理；提示覆盖后，成功结果替换旧文本，失败时保留旧文本，`attempts` 和已知 `usage` 累计。若处理范围涉及未保存草稿，前端必须先让用户保存或处理提示，不可静默丢弃。处理所选范围时，书籍状态保持 `processing` 或 `pausing`，直到本次处理结束；所选页成功而其他页面仍为未处理状态时，书籍回到 `uploaded` 且无错误。暂停时书籍先进入 `pausing`，当前页结束后进入 `paused`。暂停保留已有结果及请求用量；再次点击“处理”按当前选择范围发起，不承诺恢复此前队列。已暂停状态在重启后保留。

单页预览、整书预览和独立 HTML 共用页眉、正文、页脚结构；每条页眉或页脚语段按模型输出的 `alignment`、`row`、`font_size`、`bold`、`italic` 排版，不提供整页预设，也不在 `localStorage` 保存排版选项。来源页序号位于纸张外并在打印时隐藏。整书预览不提供 Markdown 下载按钮；`GET /api/books/{id}/export.md` 接口仍保留，按源页顺序合并已保存正文。页眉页脚格式通过 JSON 和 HTML 导出保留。

设置中的 `reasoning_effort` 是可选字符串，默认值为空。空值时识别和连接测试请求省略 `reasoning`；非空时传递 `reasoning: {"effort": value}`。不限制输入枚举，实际支持情况由服务和模型决定。`max_output_tokens` 为正整数，默认 `12000`，同时用于页面识别和文本连接测试，额度包含推理和正文；截断提示应提高额度或降低推理，已返回 usage 仍计入。删除接口成功返回 `204 No Content`；未知书籍返回 `404`，正在处理的同一本书返回 `409`。删除只作用于目标书籍，不改变其他书籍、全局设置或 API 密钥。

前端的“填入 DeepSeek 配置”只更新草稿，不保存、不发请求，预填 `https://api.deepseek.com`、`/responses`、`deepseek-flash`、`high` 和 `12000`；API 密钥保持不变。页面请求使用 base64 PNG `input_image` 与 `text.format=json_schema`；旧 `structured_output` 设置字段仅用于兼容。通用客户端仍保持非流式 Responses 请求，不引入 provider 框架。连接测试是文本检查，不代表图像识别质量；mock 验证不等于真实服务联调。

## 安全与限制

- 默认监听 `127.0.0.1`；这是本地单用户工具，不提供账号或多用户权限。
- API 密钥按用户授权保存到 SQLite 的独立凭据记录中，本机明文存储；启动时加载，不进入前端存储、书籍导出和日志。空白/省略保留，非空替换，`clear_api_key=true` 优先清除；设置和凭据在同一事务中提交，失败时不更新内存。升级到支持持久化密钥的版本后，重启新版并重新输入保存一次旧内存 key 即可，之后不必每次重启重填。
- API 根地址限制为 `http(s)`，拒绝 userinfo、query、fragment；请求不自动重定向，避免泄漏 Authorization。
- 模型输出按不可信输入检查响应状态和文本长度；拒绝内容或 incomplete 响应应成为可见失败。
- 运行中的任务重启后标记 `interrupted`；有限重试只覆盖暂时错误，不做无限重试。
- PDF 单页文件保留源页尺寸、旋转和裁切设置；旧书缺少单页 PDF 时，只为本次所选页面补齐。
- 不依赖 Redis、Celery、微服务、插件系统或通用工作流引擎。

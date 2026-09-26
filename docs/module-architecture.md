# 架构模块说明

项目按“本地前后端分离、逐页处理、文本校对和导出”的边界组织。根目录 [ARCHITECTURE.md](../ARCHITECTURE.md) 保留既有架构记录；本文维护当前处理范围、状态和 HTTP 接口要求，并解释运行时职责。

## 模块与责任

| 模块 | 责任 | 关键边界 |
| --- | --- | --- |
| `frontend/` | React + TypeScript + Vite 界面；项目、多文件上传确认、页面编排、轮询、文本校对、预览、导出和项目删除 | 上传确认及编排确认完成后才开放校对与整书预览；不保存 API 密钥到浏览器 |
| `frontend/src/pageSelection.ts` | 将单数页、偶数页或自定义页码解析为明确的页码列表 | 源文件从 1 计数；校验范围端点后展开，去重并升序；任一错误返回空列表和中文错误 |
| HTTP API | FastAPI 路由、请求校验、状态和错误文本 | 所有接口使用 `/api` 前缀；ID、页码和路径必须验证 |
| 导入与页面文件 | 一个项目包含多个来源文件，按 PDF / 图片分类；PDF 上传仅读取页数、保存源文件和轻量页面记录；内容预览独立按需缓存，识别时准备单页 PDF 与 PNG | 来源存入各自目录，避免同名冲突；预览最大边 1200 px；每个文件限制 100 MB，保留像素限制 |
| 持久化 | SQLite 保存设置、独立凭据、书籍及纸型、页面文本、用量和尝试次数；文件系统保存源 PDF、单页 PDF 和 PNG | 默认目录为 `backend/data/`，可用 `EBOOK_OCR_DATA_DIR` 覆盖；凭据记录在 SQLite 中本机明文保存，其他密钥文件不落盘；删除书籍时一并删除其记录和文件 |
| 协议客户端 | 按 `api_protocol` 使用 OpenAI Responses（Bearer）或 Gemini 原生 `generateContent`（`x-goog-api-key`），读取各自 usage | 非流式 POST，不自动切换 Chat Completions或跟随重定向；Gemini 仅 API Key 开发者 API，无 Vertex OAuth 或多账户系统 |
| 模型列表 | 当前草稿 GET `base_url + models_path`；OpenAI 读取 `data[].id`，Gemini 分页读取支持 `generateContent` 的完整 `models/...` 名称 | 不保存草稿或密钥，不发起推理，不随输入自动请求；列表不证明图像或结构化输出能力 |
| 普通页面代理 | 判断当前页类型；内容页直接转录；封面封底只返回类型和空转录字段，立即交接 | 不按首尾页码分类；默认独立上下文，实验性复用时历史只供排版与符号参考 |
| `SpecialPageAgent` | 用独立提示词重新读取同一页图像，提取核心书目并交回最终页面结果 | 共用已配置协议、模型、地址及推理程度；同协议独立客户端关闭上下文复用，Responses 为 `store:false` 且无 `previous_response_id`，Gemini `contents` 只含当前页；不创建 Codex 任务 |
| 流程协调 | 上传与编排分别确认，持久化文件层级、文件顺序及最终页面顺序；按项目有界并发，累计尝试和已知用量 | 编排草稿确认后保存；处理或上传期间禁止重排；暂停不再启动新页，等待已开始页结束；重跑成功替换旧文本、失败保留旧文本 |
| 排版与导出 | 前端按整书纸型共用封面、封底与内容页排版，生成单页预览、清单预览和独立 HTML；另提供包含书目的 Markdown 与固定字段 JSON | 封面封底自动使用专用版式，纸型及页边距同步到打印；页眉页脚遵循整页锚点、绝对行号与各语段字样；不精确复刻源版面或承诺每个源页对应一张打印纸；PDF 使用浏览器打印 |

## 数据流

```text
新建项目 ───────► Book（可为空）
批量上传 ───────► SourceFile[]、源文件、轻量 Page；图片生成 PNG
       │
       ▼
确认上传 → 统一文件树排序与嵌入 → 文件内选页与排序 → 跨文件页面混排
       │
       ▼
按需原页预览 ───► 独立缩略图缓存；不准备 OCR 资产，不调用模型
确认页面编排 ───► 保存文件层级、文件序和最终页序，校对与导出仅使用已选页
       │
       ▼
逐页准备所选页 ─► PDF 仅补该页缺少的单页 PDF、PNG；更新实际宽高
       │
       ▼
普通页面代理 ──► content：直接返回正文、页眉页脚
       │
       └───────► front_cover / back_cover：结束普通转录
                              │
                              ▼
                 SpecialPageAgent：独立读取同图核心书目
                              │
                              ▼
整页成功保存 ──► Page 类型、书目、正文、页眉页脚 + 两阶段 Usage / attempts
       │
       ▼
自动填入校对栏 ► 按页面类型及整书纸型渲染
       │
       ▼
人工校对 ───────► PUT /api/books/{id}/pages/{number}
       │
       ├─────────► Markdown / JSON
       └─────────► 内嵌 CSS 的 HTML ─► 浏览器打印 PDF
```

页面代理每次提供文件名、页码、总页数、本页图片和转录指令；复用历史仅供排版与符号参考，只输出当前页，不复制历史正文或补写跨页内容。页面中的命令都属于待转录资料，不改变任务。代理先依据当前图像判断 `page_kind`：`content`、`front_cover` 或 `back_cover`，不能因为第一页、最后一页、文件名或相邻页信息而认定封面封底；扉页、版权页、目录及不确定页面仍是 `content`。只处理原书排印内容，排除后加手写批注等类似笔迹。

普通页面代理按 `PAGE_RESPONSE_SCHEMA` 返回五个必填字段：`page_kind`、`page_side`、`header_segments`、`body_markdown`、`footer_segments`。内容页在该请求中直接完成转录；特殊类型返回 `front_cover` 或 `back_cover`、`page_side: unknown`，正文为 `""`、页眉页脚为 `[]`，不提取书目，也不继续读取其他文字。后端收到分流标记后立即调用独立 `SpecialPageAgent`，向新的客户端提供同一张本页图像、来源信息和分流类型。它采用 `SPECIAL_PAGE_AGENT_PROMPT` 与 `SPECIAL_PAGE_RESPONSE_SCHEMA`，只返回必填的 `page_kind`（`front_cover|back_cover`）和 `cover_fields`。两个阶段均使用严格 JSON Schema；请求仍为非流式，分流发生在首个响应返回后，不承诺流式实时中断。

特殊子代理按 `ResponsesConfig` 或 `GeminiConfig` 创建同协议独立客户端，固定 `context_reuse_enabled=False`，两种客户端均提供 `request_special_page`。Responses 专用请求发送 `store:false`，不传 `previous_response_id`；Gemini 专用请求的 `contents` 只含当前页，不携带普通代理历史。两者都不携带相邻页或第一阶段转录文本，专用响应不写入普通代理历史；协议、模型、接入地址和推理程度沿用同一任务设置，不新增模型选项或代理框架。`cover_fields` 为按原页阅读顺序排列的 `{kind,text}` 数组；`kind` 只接受 `title`、`subtitle`、`author`、`translator`、`editor`、`publisher`、`series`、`edition`、`publication_year`、`isbn`，`text` 为纯文本。只填本页可见的核心书目，排除简介、宣传、推荐语、定价、联系方式、网址等其他内容，不从上下文或常识补全；没有可辨核心信息的封底可保留空书目数组。

后端将专用提取结果合并为 Page 结构，封面封底的正文与页眉页脚为空，`page_side` 为 `unknown`，内容页的 `cover_fields` 为空数组。中间分流结果不落库，也不将第一阶段计作页面完成；整页成功后才保存最终结果，书目自动填入既有校对栏，按类型及当前纸型渲染，无须复制或额外点击。特殊阶段失败保留旧结果。普通页通常一次请求，特殊页通常两次；两阶段及各自重试的实际 usage 和 attempts 累计在同一页。暂停等待已开始的整页（包括专用提取）结束。

内容页按原页阅读顺序把页眉、正文、页脚分开，脚注和图注留在正文；不可读的原文局部写 `[无法辨认]`，空白内容页返回空语段和空正文。

整书预览的“打印版本”选项默认关闭；启用时前端将临时 `printVersion` 同步传给整书预览、独立 HTML 和打印样式。已知左右页采用固定源页侧别的装订边距，长正文续页沿用该源页版式；未知页侧及封面封底居中，不插入凑左右面的空白页。该选项不修改识别数据或项目设置，六纸型规则见[左右页与打印版](PRINT_LAYOUT.md)。

每条页眉或页脚语段包含必填的 `kind`、`text`、`alignment`、`row`、`font_size`、`bold`、`italic`。`alignment` 为 `left|center|right`，表示相对于整页可排印宽度的锚点，不按语段数量重新平均分列。`row` 为各自区域内 1–10 的绝对行号，同一视觉行共用行号，缺失行保留为空白，不能压缩成连续索引；字号为 `small|normal`，粗体和斜体分别作用于每条语段。旧保存结果缺少格式字段时使用居中、第一行、小号字、非粗体、非斜体的默认值。只有正文使用 Markdown；正文中的公式行内使用 `$...$`，独立公式的 `$$` 分隔符各自独占一行，中间保留 LaTeX。模型 `body_markdown` 映射为 API `text`，其余字段保留名称并分别持久化。

`Page` 固定包含 `number`、`source_id`、`source_filename`、`source_page`、`status`、`error`、`page_kind`、`page_side`、`cover_fields`、`text`、`header_segments`、`footer_segments`、`usage` 和 `attempts`。`number` 是项目内永久页面 ID，排序不重编号；原文件页码用 `source_page` 表示。SQLite 增量添加页面类型与书目列，旧页默认 `content` 和空书目；`pages.page_side` 为 `TEXT NOT NULL DEFAULT 'unknown'`，取值 `left|right|unknown`。迁移保留旧正文、页眉页脚、用量及状态，不推断旧页侧、不自动重新识别。模型页侧规则及打印用途见[左右页与打印版](PRINT_LAYOUT.md)。`Usage` 为 `{input_tokens, output_tokens, total_tokens, complete}`：供应商没有返回的字段保持 `null`，不会估算或显示为零；一次或多次重试只累加已知值，任意尝试缺少用量或无法确认消耗时 `complete` 为 `false`。`Book.usage` 是项目全部已尝试页面的合计。

`Book` 复用为项目，`file_count` 表示来源文件数，`upload_confirmed` 和 `selection_confirmed` 分别表示上传与编排确认。`page_count` 为全部来源文件的总页数，`completed_pages` 只统计已选页中的完成数，进度分母使用 `selected_page_count`。`SourceFile` 包含 `id`、`filename`、`kind:pdf|image`、`page_count`、`position` 和 `parent_id:string|null`；`null` 表示根级文件，`position` 仍为项目内全局文件顺序。`BookDetail` 返回 `{book,files,pages}`，`pages` 是按最终顺序排列的已选页；`Arrangement` 另提供全部来源页及 `order`（已选永久 ID 的有序数组）。不可用文件层级、`Page.number` 或 `source_page` 重新排序导出，`page_order` 始终是最终页面顺序的唯一依据。

`Book.paper_size` 为 `a4|a5|a6|b5|b6|trade_6x9`，默认 `a4`，项目列表、详情及 JSON 导出均携带该字段。尺寸依次为 210 × 297、148 × 210、105 × 148、176 × 250、125 × 176、152.4 × 228.6 毫米；B5、B6 使用 ISO 尺寸。SQLite 增量添加该列，旧项目采用 A4，保留原页面、状态和用量。纸型用于整书重排，不改变源图比例或 OCR 输入。

编排保存 `{file_order:string[],page_order:number[],file_parents?:Record<string,string|null>}`：文件序必须完整且无重复，页面序必须非空、合法且无重复，允许不同文件的页面交错。新版前端提交完整 `file_parents` 映射，其键必须恰好覆盖项目全部文件；父 ID 必须属于同一项目且自身为根级，拒绝自指、父链循环及子文件下继续嵌套。文件层级只允许根文件与一层子文件，所有子文件同级。前端将文件树按深度优先顺序平铺为 `file_order`，后端保存传入全局顺序，不额外强制树遍历规则。文件位置、父关系与所选页面顺序在同一事务内保存。旧调用省略 `file_parents` 时保留已有父关系；新上传文件和无父关系的旧数据默认根级。初始化时将已有深层后代平铺为同项目顶级祖先的直接子文件，只更新父关系，保留全局文件位置、页序与所有结果；已有单层结构及重复初始化不受影响。

移除页面只改变清单，源文件、页面记录、历史结果、缓存及用量保留；重新选入 `ready` 页恢复原结果。追加文件重置两道确认标记，保留原已选顺序与旧结果。旧单文件数据迁移为一个来源文件，已有页面 ID、缓存、结果和用量继续沿用。

## HTTP 契约索引

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 返回 `{"status":"ok"}` |
| GET / PUT | `/api/settings` | 读取或保存非密钥设置；空密钥表示保留，`clear_api_key=true` 清除 |
| POST | `/api/settings/test` | 使用已保存设置主动测试连接 |
| POST | `/api/settings/models` | 使用当前草稿获取模型 ID，返回 `{models:string[]}`，不保存设置 |
| GET | `/api/books` | 项目摘要列表 |
| POST | `/api/projects` | `{"title":"项目名称"}` 创建空项目，返回 `Book` |
| POST | `/api/books/{id}/files` | multipart `files` 批量追加 PDF/PNG/JPEG，返回 `Arrangement`；整批校验通过后入库 |
| POST | `/api/books/{id}/confirm-upload` | 确认非空文件清单，返回 `Arrangement` |
| GET / PUT | `/api/books/{id}/arrangement` | GET 返回含父关系的来源文件、全部来源页及已选 `order`；PUT 保存 `{file_order,page_order,file_parents?}` 并确认编排，返回 `BookDetail` |
| GET | `/api/books/{id}/pages/{number}/preview` | 按永久页面 ID 获取原页 PNG 预览；按需生成独立缓存，不调用模型 |
| POST | `/api/books` | 旧单文件 multipart `file` 导入接口，保留兼容 |
| GET | `/api/books/{id}` | 项目、来源文件和按最终顺序排列的已选页面 |
| PUT | `/api/books/{id}/layout` | 保存整书纸型 `{"paper_size":"a5"}`，返回 `Book`；允许识别期间修改，不改变内容或处理状态，不调用模型 |
| POST | `/api/books/{id}/process` | 新项目必须已确认上传与编排；可提交已选 ID 子集 `{"pages":[3,1]}`，实际按编排顺序处理；省略 `pages` 时处理全部已选页（包括 `ready`）；运行中返回 409。旧单文件接口保留首次选择兼容 |
| POST | `/api/books/{id}/pages` | 兼容接口，`{"pages":[StrictInt,...]}` 按永久页面 ID 追加到清单末尾，返回 `BookDetail`；首次确认前或越界添加返回 400 |
| DELETE | `/api/books/{id}/pages/{number}` | 单页移出清单，返回 `BookDetail`；首次确认前返回 400，当前 `processing` 页返回 409；允许移除最后一页 |
| POST | `/api/books/{id}/pause` | 请求暂停；不再启动新页，所有已开始页结束后暂停，未运行时返回 409 |
| DELETE | `/api/books/{id}` | 删除单本书及其页面、请求用量记录和文件；运行中返回 409 |
| PUT | `/api/books/{id}/pages/{number}` | 保存校对结果：`text` 必填，`page_kind`、`cover_fields` 可选；旧 `{text}` 请求兼容 |
| GET | `/api/books/{id}/export` | 确认编排后下载按最终顺序排列的 `{book,files,pages}` 结构化 JSON |
| GET | `/api/books/{id}/export.md` | 确认编排后下载按最终页序合并内容页正文及封面封底书目的 Markdown 文件 |
| GET | `/api/books/{id}/assets/{name}` | 安全范围内读取已保存页面文件 |

所有响应字段都应保留契约约定的 nullable 字段。错误使用 FastAPI 的 `detail` 文本。前端通过轮询观察状态，不引入 WebSocket。

纸型更新要求非空且属于上述枚举的 `paper_size`，非法值返回 `422`，不存在的项目返回 `404`。该接口独立于单页校对和编排确认，不重置上传或编排标记，不修改正文、书目、页眉页脚、用量或尝试次数。校对工具栏选择后即保存当前项目纸型，不需要重新识别页面。

页面校对请求的可选 `page_kind`、`cover_fields` 可以省略，不能显式为 `null`。内容页不接受非空书目；封面封底不接受非空 `text`，保存时清空页眉页脚并将 `page_side` 清为 `unknown`。切回内容页时清空书目，正文由本次 `text` 提供，页侧仍为 `unknown`；正文校对保留已有页侧，不开放页侧编辑字段。省略类型时沿用已保存类型，省略书目时封面封底保留已有书目；保存不改变 `usage` 或 `attempts`。前端提供页面类型纠正及封面书目增删和文本编辑，运行中拒绝保存。

新界面统一在页面编排中维护选页和顺序；PDF 和图片在同一文件树中排序，类型仅作标识。文件排序立即反映到最终页面草稿；嵌入文件在父文件下显示为子级，移动文件时连同子文件整组移动并保留已有选页范围。根文件携带子文件移入另一根文件时，整组成员都成为目标根文件的直接子文件，不增加层级；子文件不能作为嵌入目标。文件内视图展示当前父文件与全部子文件页面，局部页面排序只置换当前文件组占用的成书位置，保留组外页面位置。嵌入默认保持已编入范围；来源文件尚未编入页面时，由用户明确选择是否编入全部页，不隐式扩大范围。页码范围按当前来源文件从 1 开始计数，不使用印刷页码；范围解析可以去重升序，但最终页面顺序以用户编排为准，不在保存或识别时重新升序。页面预览按需请求，网格不一次性渲染整份 PDF。

旧页面增删 API 保留兼容：运行中的流程通过共享 `pending_pages` 消费队列，追加未完成页可入队，移出的未开始页跳过，当前识别页不可移除。新界面在闲置时通过编排统一修改，不在处理期间重排。“识别未完成页”由前端明确提交未完成页；全部 `ready` 时，经确认可“重新识别全部页”。成功替换旧文本，失败保留旧文本，`attempts` 和已知 `usage` 累计。未保存草稿需先保存或处理提示。项目保持 `processing` 或 `pausing` 直到本次处理结束；暂停时不再启动新页，等待所有已开始页结束后进入 `paused`，保留结果和用量，已暂停状态在重启后保留。新任务（含暂停后继续）重新分组，不复用旧响应 ID。

单页预览、整书预览、独立 HTML 和打印共用所选纸型及页面排版：模型返回或人工修正 `page_kind` 后自动采用对应规则；封面按标题、署名和出版信息分层，封底书目靠底，内容页使用页眉、正文、页脚结构。纸型改变纸张尺寸、页边距及可排内容宽度，封面字号和留白随纸型适配。页眉置顶、页脚置底、正文自然伸展；每条语段按模型给出的整页 `alignment` 锚点、`row` 绝对行号以及 `font_size`、`bold`、`italic` 排版，保留缺失行的空白。项目纸型保存在后端，不在 `localStorage` 保存排版选项。来源文件、源页码和版式提示位于纸张外并在打印时隐藏，不加入书内内容。整书预览不提供 Markdown 下载按钮；`GET /api/books/{id}/export.md` 按最终页序合并内容页正文及带类型标题、字段标签的封面封底书目。JSON 保留纸型、页面类型、书目及页眉页脚结构，HTML 内嵌所选纸型的尺寸、页边距与打印规则；位置契约不表示复刻原页像素坐标，长正文仍可跨打印页。

设置中的 `processing_concurrency` 为正整数，默认 10，无固定上限；调度有界，每个项目同时处理页数不超过配置，多个项目各自限制。`context_reuse_enabled` 为布尔值，默认 `false`；`context_reuse_max_pages` 为 1–10 的整数，默认 10，含首张。旧设置缺字段使用默认值。保存设置不联网，每次任务启动时固定设置快照，修改下次任务生效。开启实验性复用后，按本次待处理页的最终编排顺序连续分组，组内串行、不同组并行。每组普通代理使用独立的所选协议客户端：OpenAI 发送 `store:true`，以 `previous_response_id` 续接成功响应；Gemini 在本地保存完整 `contents`（图片和原始模型 `thoughtSignature` 一并保留）并随下一次请求发送，不使用服务端 `cachedContent`。特殊子代理始终使用另一个同协议独立客户端并关闭复用：Responses 为 `store:false` 且无父响应 ID，Gemini 的 `contents` 只含当前页；专用响应不接入普通代理历史链。默认普通代理使用独立上下文，OpenAI 为 `store:false`，Gemini 只含当前页。OpenAI 兼容服务须支持保存与续接，缺少响应 ID 时报错，不静默降级；两种协议页面失败均保留旧结果并重置对话，下一页从新上下文开始。历史仍占用上下文与用量，不保证费用降低。完成可能乱序，呈现与导出仍遵循编排。

设置中的 `reasoning_effort` 是可选字符串，默认空值使用服务默认。OpenAI 非空时传递 `reasoning: {"effort": value}`，不限制枚举；Gemini 将 `minimal/low/medium/high` 映射为 `thinkingConfig.thinkingLevel`（Gemini 3），将不小于 `-1` 的整数映射为 `thinkingConfig.thinkingBudget`（Gemini 2.5；`-1` 动态，`0` 关闭是否可用取决于模型）。客户端不按模型名猜测参数。识别和连接测试均不发送最大输出 token 设置（OpenAI `max_output_tokens`、Gemini `maxOutputTokens`），旧保存值不生效；额度遵循服务默认和模型限制。截断时提示检查输出限制或降低推理程度，已返回 usage 仍计入。删除整本书接口成功返回 `204 No Content`；未知书籍返回 `404`，正在处理的同一本书返回 `409`。删除只作用于目标书籍，不改变其他书籍、全局设置或 API 密钥。

设置默认 `api_protocol=openai_responses`、`base_url=https://api.openai.com/v1`、`responses_path=/responses`、`models_path=/models`；旧设置缺协议时沿用 OpenAI，缺模型路径时采用 `/models`。Gemini 协议为 `gemini`，官方根地址 `https://generativelanguage.googleapis.com/v1beta`、模型集合路径 `/models`，无具体模型预设。切换协议不联网，默认服务地址可随协议替换，自定义地址保留。OpenAI 两个相对路径留空时直连根地址；Gemini 不使用 `responses_path`，`models_path` 表示资源集合，空值表示根地址自身就是集合。生成地址为集合加 `/`、编码后的短模型名及 `:generateContent`，手动模型 ID 接受裸名称或 `models/` 前缀。

`POST /api/settings/models` 请求体为 `{api_protocol?:"openai_responses"|"gemini",base_url:string,models_path:string,api_key?:string,clear_api_key?:boolean,timeout_seconds:number}`。前端提交当前草稿，无须先保存；OpenAI 以 Bearer GET 读取 `data[].id`。Gemini 以 `x-goog-api-key` GET 模型集合，跟随 `nextPageToken`（下次传 `pageToken`），仅返回 `supportedGenerationMethods` 包含 `generateContent` 的完整 `name`，结果仍为 `{models:string[]}`。空密钥仅在规范化后的根地址、协议都与已保存配置一致且未清除时复用。获取不修改设置或凭据、不发起推理、不随输入自动联网；失败可手填模型 ID，列表不证明图像或 JSON Schema 能力，敏感上游响应不透传。

`PUT /api/settings` 在有旧密钥且规范化根地址或协议变化时，要求新密钥或 `clear_api_key=true`，否则返回 `400`；没有旧密钥时允许保存空配置。同一连接身份下空白密钥可保留旧值，显式清除优先。读取设置不返回密钥。

普通与特殊页请求均按显式协议选择客户端。OpenAI 使用 base64 PNG `input_image` 与 `text.format` 的 `json_schema`（`strict: true`）；Gemini 使用原生 `systemInstruction`、`contents`/`inlineData` 及 `generationConfig`，通过 `responseMimeType: application/json` 和 `responseJsonSchema` 请求结构化输出，不发送 OpenAI 参数。旧 `structured_output` 字段仅用于兼容。连接测试在保存后由用户主动触发非流式文本推理，可能产生用量，不代表图像识别质量。

Gemini 输入 token 取 `usageMetadata.promptTokenCount`，输出取 `candidatesTokenCount + thoughtsTokenCount`（thoughts 缺失按 0；candidates 未知则输出未知），总量取 `totalTokenCount`，未知字段保持未知，不估算。协议参考：[Google 模型列表](https://ai.google.dev/api/models)、[Google generateContent](https://ai.google.dev/api/generate-content)。

## 安全与限制

- 默认监听 `127.0.0.1`；这是本地单用户工具，不提供账号或多用户权限。
- API 密钥按用户授权保存到 SQLite 的独立凭据记录中，本机明文存储；启动时加载，不进入前端存储、书籍导出和日志。空白/省略保留，非空替换，`clear_api_key=true` 优先清除；设置和凭据在同一事务中提交，失败时不更新内存。升级到支持持久化密钥的版本后，重启新版并重新输入保存一次旧内存 key 即可，之后不必每次重启重填。
- API 根地址限制为 `http(s)`，拒绝 userinfo、query、fragment；请求不自动重定向，避免泄漏 Authorization。
- 模型输出按不可信输入检查响应状态和文本长度；拒绝内容或 incomplete 响应应成为可见失败。
- 运行中的任务重启后标记 `interrupted`；有限重试只覆盖暂时错误，不做无限重试。
- PDF 上传仅建立宽高为 0 的轻量页面记录。原页预览独立按需渲染；识别时逐页更新实际宽高并补齐所选页缺少的 PDF、PNG，单页文件保留源页尺寸、旋转和裁切，已有资产复用。上传、选择和预览均不发起模型请求。
- 不依赖 Redis、Celery、微服务、插件系统或通用工作流引擎。

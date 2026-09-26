# 架构模块说明

项目按“本地前后端分离、逐页处理、文本校对和导出”的边界组织。根目录 [ARCHITECTURE.md](../ARCHITECTURE.md) 保留既有架构记录；本文维护当前处理范围、状态和 HTTP 接口要求，并解释运行时职责。

## 模块与责任

| 模块 | 责任 | 关键边界 |
| --- | --- | --- |
| `frontend/` | React + TypeScript + Vite 界面；项目、多文件上传确认、页面编排、轮询、文本校对、预览、导出和项目删除 | 上传确认及编排确认完成后才开放校对与整书预览；不保存 API 密钥到浏览器 |
| `frontend/src/pageSelection.ts` | 将单数页、偶数页或自定义页码解析为明确的页码列表 | 源文件从 1 计数；校验范围端点后展开，去重并升序；任一错误返回空列表和中文错误 |
| HTTP API | FastAPI 路由、请求校验、状态和错误文本 | 所有接口使用 `/api` 前缀；ID、页码和路径必须验证 |
| 导入与页面文件 | 一个项目包含多个来源文件，按 PDF / 图片分类；PDF 上传仅读取页数、保存源文件和轻量页面记录；内容预览独立按需缓存，识别时准备单页 PDF 与 PNG | 来源存入各自目录，避免同名冲突；预览最大边 1200 px；每个文件限制 100 MB，保留像素限制 |
| 持久化 | SQLite 保存设置、独立凭据、书籍、页面文本、用量和尝试次数；文件系统保存源 PDF、单页 PDF 和 PNG | 默认目录为 `backend/data/`，可用 `EBOOK_OCR_DATA_DIR` 覆盖；凭据记录在 SQLite 中本机明文保存，其他密钥文件不落盘；删除书籍时一并删除其记录和文件 |
| Responses 客户端 | 组合 `base_url + responses_path`，发起带 Bearer 的非流式 POST 并读取供应商 usage | 只支持通用 Responses API；不自动切换 Chat Completions、不开 provider 框架或跟随重定向 |
| 模型列表 | 按用户操作，使用当前设置草稿请求 `base_url + models_path`，通过带 Bearer 的 GET 读取 `data[].id` | 不保存草稿或密钥，不发起 Responses 推理，不随输入自动请求；列表不证明模型能力 |
| 页面代理 | 判断当前页类型；封面封底输出核心书目，内容页输出结构化页眉、正文 Markdown 和页脚 | 不按首尾页码分类；默认独立上下文，实验性复用时历史只供排版与符号参考；不创建 Codex 任务 |
| 流程协调 | 上传与编排分别确认，持久化文件层级、文件顺序及最终页面顺序；按项目有界并发，累计尝试和已知用量 | 编排草稿确认后保存；处理或上传期间禁止重排；暂停不再启动新页，等待已开始页结束；重跑成功替换旧文本、失败保留旧文本 |
| 排版与导出 | 前端共用封面、封底与内容页排版，生成单页预览、清单预览和独立 HTML；另提供包含书目的 Markdown 与固定字段 JSON | 页眉页脚遵循整页锚点、绝对行号与各语段字样；不精确复刻源版面或承诺每个源页对应一张打印纸；PDF 使用浏览器打印 |

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
页面代理 ───────► Page 类型、书目、正文、页眉页脚 + Usage + attempts
       │
       ▼
人工校对 ───────► PUT /api/books/{id}/pages/{number}
       │
       ├─────────► Markdown / JSON
       └─────────► 内嵌 CSS 的 HTML ─► 浏览器打印 PDF
```

页面代理每次提供文件名、页码、总页数、本页图片和转录指令；复用历史仅供排版与符号参考，只输出当前页，不复制历史正文或补写跨页内容。页面中的命令都属于待转录资料，不改变任务。代理先依据当前图像判断 `page_kind`：`content`、`front_cover` 或 `back_cover`，不能因为第一页、最后一页、文件名或相邻页信息而认定封面封底；扉页、版权页、目录及不确定页面仍是 `content`。只处理原书排印内容，排除后加手写批注等类似笔迹。

模型返回必填的 `page_kind`、`cover_fields`、`header_segments`、`body_markdown`、`footer_segments`，后端按严格 JSON Schema 请求并校验。`cover_fields` 为按原页阅读顺序排列的 `{kind,text}` 数组；`kind` 只接受 `title`、`subtitle`、`author`、`translator`、`editor`、`publisher`、`series`、`edition`、`publication_year`、`isbn`，`text` 为纯文本。封面封底只填本页可见的核心书目，正文为 `""`、页眉页脚为 `[]`；排除简介、宣传、推荐语、定价、联系方式、网址等其他内容，不推测缺失信息。没有可辨核心信息的封底可保留空书目数组。内容页的 `cover_fields` 必须为 `[]`，按原页阅读顺序把页眉、正文、页脚分开，脚注和图注留在正文；不可读的原文局部写 `[无法辨认]`，空白内容页返回空语段和空正文。

每条页眉或页脚语段包含必填的 `kind`、`text`、`alignment`、`row`、`font_size`、`bold`、`italic`。`alignment` 为 `left|center|right`，表示相对于整页可排印宽度的锚点，不按语段数量重新平均分列。`row` 为各自区域内 1–10 的绝对行号，同一视觉行共用行号，缺失行保留为空白，不能压缩成连续索引；字号为 `small|normal`，粗体和斜体分别作用于每条语段。旧保存结果缺少格式字段时使用居中、第一行、小号字、非粗体、非斜体的默认值。只有正文使用 Markdown；正文中的公式行内使用 `$...$`，独立公式的 `$$` 分隔符各自独占一行，中间保留 LaTeX。模型 `body_markdown` 映射为 API `text`，其余字段保留名称并分别持久化。

`Page` 固定包含 `number`、`source_id`、`source_filename`、`source_page`、`status`、`error`、`page_kind`、`cover_fields`、`text`、`header_segments`、`footer_segments`、`usage` 和 `attempts`。`number` 是项目内永久页面 ID，排序不重编号；原文件页码用 `source_page` 表示。SQLite 增量添加页面类型与书目列，旧页默认 `content` 和空书目，保留旧正文、页眉页脚、用量及状态，不自动重新识别。`Usage` 为 `{input_tokens, output_tokens, total_tokens, complete}`：供应商没有返回的字段保持 `null`，不会估算或显示为零；一次或多次重试只累加已知值，任意尝试缺少用量或无法确认消耗时 `complete` 为 `false`。`Book.usage` 是项目全部已尝试页面的合计。

`Book` 复用为项目，`file_count` 表示来源文件数，`upload_confirmed` 和 `selection_confirmed` 分别表示上传与编排确认。`page_count` 为全部来源文件的总页数，`completed_pages` 只统计已选页中的完成数，进度分母使用 `selected_page_count`。`SourceFile` 包含 `id`、`filename`、`kind:pdf|image`、`page_count`、`position` 和 `parent_id:string|null`；`null` 表示根级文件，`position` 仍为项目内全局文件顺序。`BookDetail` 返回 `{book,files,pages}`，`pages` 是按最终顺序排列的已选页；`Arrangement` 另提供全部来源页及 `order`（已选永久 ID 的有序数组）。不可用文件层级、`Page.number` 或 `source_page` 重新排序导出，`page_order` 始终是最终页面顺序的唯一依据。

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

页面校对请求的可选 `page_kind`、`cover_fields` 可以省略，不能显式为 `null`。内容页不接受非空书目；封面封底不接受非空 `text`，保存时清空页眉页脚。切回内容页时清空书目，正文由本次 `text` 提供。省略类型时沿用已保存类型，省略书目时封面封底保留已有书目；保存不改变 `usage` 或 `attempts`。前端提供页面类型纠正及封面书目增删和文本编辑，运行中拒绝保存。

新界面统一在页面编排中维护选页和顺序；PDF 和图片在同一文件树中排序，类型仅作标识。文件排序立即反映到最终页面草稿；嵌入文件在父文件下显示为子级，移动文件时连同子文件整组移动并保留已有选页范围。根文件携带子文件移入另一根文件时，整组成员都成为目标根文件的直接子文件，不增加层级；子文件不能作为嵌入目标。文件内视图展示当前父文件与全部子文件页面，局部页面排序只置换当前文件组占用的成书位置，保留组外页面位置。嵌入默认保持已编入范围；来源文件尚未编入页面时，由用户明确选择是否编入全部页，不隐式扩大范围。页码范围按当前来源文件从 1 开始计数，不使用印刷页码；范围解析可以去重升序，但最终页面顺序以用户编排为准，不在保存或识别时重新升序。页面预览按需请求，网格不一次性渲染整份 PDF。

旧页面增删 API 保留兼容：运行中的流程通过共享 `pending_pages` 消费队列，追加未完成页可入队，移出的未开始页跳过，当前识别页不可移除。新界面在闲置时通过编排统一修改，不在处理期间重排。“识别未完成页”由前端明确提交未完成页；全部 `ready` 时，经确认可“重新识别全部页”。成功替换旧文本，失败保留旧文本，`attempts` 和已知 `usage` 累计。未保存草稿需先保存或处理提示。项目保持 `processing` 或 `pausing` 直到本次处理结束；暂停时不再启动新页，等待所有已开始页结束后进入 `paused`，保留结果和用量，已暂停状态在重启后保留。新任务（含暂停后继续）重新分组，不复用旧响应 ID。

单页预览、整书预览、独立 HTML 和打印共用页面排版：封面与封底各自使用独立书目版式，内容页使用页眉、正文、页脚结构。页眉置顶、页脚置底、正文自然伸展；每条语段按模型给出的整页 `alignment` 锚点、`row` 绝对行号以及 `font_size`、`bold`、`italic` 排版，保留缺失行的空白。不提供整页预设，也不在 `localStorage` 保存排版选项。来源文件和源页码位于纸张外并在打印时隐藏。整书预览不提供 Markdown 下载按钮；`GET /api/books/{id}/export.md` 按最终页序合并内容页正文及带类型标题、字段标签的封面封底书目。JSON 保留页面类型、书目及页眉页脚结构，HTML 保留对应版式；位置契约不表示复刻原页像素坐标。

设置中的 `processing_concurrency` 为正整数，默认 10，无固定上限；调度有界，每个项目同时处理页数不超过配置，多个项目各自限制。`context_reuse_enabled` 为布尔值，默认 `false`；`context_reuse_max_pages` 为 1–10 的整数，默认 10，含首张。旧设置缺字段使用默认值。保存设置不联网，每次任务启动时固定设置快照，修改下次任务生效。开启实验性复用后，按本次待处理页的最终编排顺序连续分组，组内串行、不同组并行；每组独立 `ResponsesClient`，发送 `store:true`，以 `previous_response_id` 续接成功响应。默认独立上下文发送 `store:false`，不续接。兼容服务须支持保存与续接，缺少响应 ID 时报错，不静默降级；页面失败保留旧结果并重置对话，下一页从新上下文开始。历史仍占用上下文与用量，不保证费用降低。完成可能乱序，呈现与导出仍遵循编排。

设置中的 `reasoning_effort` 是可选字符串，默认值为空。空值时识别和连接测试请求省略 `reasoning`；非空时传递 `reasoning: {"effort": value}`。不限制输入枚举，实际支持情况由服务和模型决定。应用不提供最大输出 token 设置，页面识别和文本连接测试均省略 `max_output_tokens`，旧保存值不再生效；输出额度遵循供应商默认行为及模型限制，不代表无限输出。截断时提示检查供应商或模型的输出限制，或降低推理程度，已返回 usage 仍计入。删除整本书接口成功返回 `204 No Content`；未知书籍返回 `404`，正在处理的同一本书返回 `409`。删除只作用于目标书籍，不改变其他书籍、全局设置或 API 密钥。

设置默认 `base_url=https://api.openai.com/v1`、`responses_path=/responses`、`models_path=/models`。两个路径分别接受自定义相对路径，空值表示直接请求根地址，保留完整端点接入方式。`models_path` 随设置持久化，旧设置缺字段时使用默认值。

`POST /api/settings/models` 请求体为 `{base_url:string,models_path:string,api_key?:string,clear_api_key?:boolean,timeout_seconds:number}`。前端“获取模型”提交当前草稿，无须先保存；后端以 Bearer 凭据 GET 拼接后的模型列表地址，从标准 `data[].id` 读取模型 ID，返回 `{models:string[]}`。空密钥仅在规范化后的根地址与已保存地址一致且 `clear_api_key` 不为真时复用已保存密钥；换地址后必须重新输入密钥。获取操作不修改已保存设置或凭据、不发起 Responses 推理；输入变化不自动联网。列表接口不可用时保留手动输入模型 ID。列表不用于推断图像、Responses 或 JSON Schema 能力，上游原始敏感响应不向界面透传。

页面请求统一使用 base64 PNG `input_image` 与 `text.format` 的 `json_schema`（`strict: true`），不按供应商域名调整协议，不提供供应商预设；旧 `structured_output` 设置字段仅用于兼容。通用客户端仍保持非流式 Responses 请求，不引入 provider 框架。连接测试在保存后由用户主动触发文本推理，可能产生用量，不代表图像识别质量。

## 安全与限制

- 默认监听 `127.0.0.1`；这是本地单用户工具，不提供账号或多用户权限。
- API 密钥按用户授权保存到 SQLite 的独立凭据记录中，本机明文存储；启动时加载，不进入前端存储、书籍导出和日志。空白/省略保留，非空替换，`clear_api_key=true` 优先清除；设置和凭据在同一事务中提交，失败时不更新内存。升级到支持持久化密钥的版本后，重启新版并重新输入保存一次旧内存 key 即可，之后不必每次重启重填。
- API 根地址限制为 `http(s)`，拒绝 userinfo、query、fragment；请求不自动重定向，避免泄漏 Authorization。
- 模型输出按不可信输入检查响应状态和文本长度；拒绝内容或 incomplete 响应应成为可见失败。
- 运行中的任务重启后标记 `interrupted`；有限重试只覆盖暂时错误，不做无限重试。
- PDF 上传仅建立宽高为 0 的轻量页面记录。原页预览独立按需渲染；识别时逐页更新实际宽高并补齐所选页缺少的 PDF、PNG，单页文件保留源页尺寸、旋转和裁切，已有资产复用。上传、选择和预览均不发起模型请求。
- 不依赖 Redis、Celery、微服务、插件系统或通用工作流引擎。

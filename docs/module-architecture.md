# 架构模块说明

项目按“本地前后端分离、逐页处理、文本校对和导出”的边界组织。根目录 [ARCHITECTURE.md](../ARCHITECTURE.md) 保留既有架构记录；本文维护当前处理范围、状态和 HTTP 接口要求，并解释运行时职责。

## 模块与责任

| 模块 | 责任 | 关键边界 |
| --- | --- | --- |
| `ocr.bat` / `scripts/start.ps1` | Windows 一键入口；真实启动前准备 Python、前端与 LaTeX 依赖，再管理本地服务 | Python / Node.js / pnpm 仍是前置条件；后端依赖仅安装到 `.venv`；按需求文件指纹和缺失模块 / Vite 决定安装，成功后缓存指纹；已有正常服务直接返回，不改其依赖 |
| `scripts/latex-dependencies.ps1` | 公开 `Initialize-LatexDependencies`；查找 XeLaTeX，必要时下载并校验官方 TinyTeX-1，补齐模板宏包、Fandol 字体与格式文件 | 显式配置无效即停止；按 PATH 与常规 TinyTeX 目录复用；完整依赖不联网，缺包时用所选 TeX Live 的 `tlmgr`，缺格式时用 `fmtutil-sys`；安装目标须为 ASCII、无空格且不覆盖已有目录；只修改启动进程环境，不编译应用文档 |
| `frontend/` | React + TypeScript + Vite 界面；项目、多文件上传确认、页面编排、轮询、文本校对、预览、导出和项目删除 | 上传确认及编排确认完成后才开放校对与整书预览；不保存 API 密钥到浏览器 |
| `frontend/src/pageSelection.ts` | 将单数页、偶数页或自定义页码解析为明确的页码列表 | 源文件从 1 计数；校验范围端点后展开，去重并升序；任一错误返回空列表和中文错误 |
| HTTP API | FastAPI 路由、请求校验、状态和错误文本 | 所有接口使用 `/api` 前缀；ID、页码和路径必须验证 |
| 导入与页面文件 | 一个项目包含多个来源文件，按 PDF / 图片分类；PDF 上传仅读取页数、保存源文件和轻量页面记录；内容预览独立按需缓存，识别时准备单页 PDF 与 PNG | 来源存入各自目录，避免同名冲突；预览最大边 1200 px；每个文件限制 100 MB，保留像素限制 |
| 持久化 | SQLite 保存设置、独立凭据、书籍纸型与排版、LaTeX 正文、结构化书目及页眉页脚、用量和尝试次数；文件系统保存原页资产与编译缓存 | 默认目录为 `backend/data/`，可用 `EBOOK_OCR_DATA_DIR` 覆盖；凭据记录在 SQLite 中本机明文保存；旧正文仅启动时迁移，写前备份；删除书籍时一并删除其记录和文件 |
| 协议客户端 | 按 `api_protocol` 使用 OpenAI Responses（Bearer）或 Gemini 原生 `generateContent`（`x-goog-api-key`），读取各自 usage | 非流式 POST，不自动切换 Chat Completions或跟随重定向；Gemini 仅 API Key 开发者 API，无 Vertex OAuth 或多账户系统 |
| 模型列表 | 当前草稿 GET `base_url + models_path`；OpenAI 读取 `data[].id`，Gemini 分页读取支持 `generateContent` 的完整 `models/...` 名称 | 不保存草稿或密钥，不发起推理，不随输入自动请求；列表不证明图像或结构化输出能力 |
| 普通页面代理 | 判断当前页类型；内容页先转录，非空正文再独立复核字重；封面封底只返回类型和空转录字段，立即交接 | 不按首尾页码分类；默认独立上下文，实验性复用时历史只供排版与符号参考 |
| `SpecialPageAgent` | 用独立提示词重新读取同一页图像，提取核心书目并交回最终页面结果 | 共用已配置协议、模型、地址及推理程度；同协议独立客户端关闭上下文复用，Responses 为 `store:false` 且无 `previous_response_id`，Gemini `contents` 只含当前页；不创建 Codex 任务 |
| 字重复核 / `backend/emphasis.py` | 对照同一原图和已有 LaTeX，只返回遗漏的字重范围；按原始字符串位置插入有界样式 | 独立客户端关闭上下文复用；不改写原文、公式或段落；精确匹配失败、重复范围或交叉范围成为页面失败，完整包含可嵌套 |
| 流程协调 | 上传与编排分别确认，持久化文件层级、文件顺序及最终页面顺序；按项目有界并发，累计尝试和已知用量 | 编排草稿确认后保存；处理或上传期间禁止重排；暂停不再启动新页，等待已开始页结束；重跑成功替换旧文本、失败保留旧文本 |
| `backend/latex_content.py` | 纯文本转义、正文命令及环境校验 | `Page.text` 只存受支持的 LaTeX 正文片段；书目与页眉页脚为纯文本；不开放宏定义、任意文件读写或外部资源命令 |
| `backend/latex_export.py` | 按整书纸型和排版设置生成完整 LaTeX，调用本机 XeLaTeX 生成 PDF | 共用 `ctexbook` + Fandol 模板；禁用 shell-escape、限制编译时间；源页分隔、长文本自然续页，不额外生成标题、目录或自动编号 |
| PDF 预览与导出 | 前端主动请求本页草稿或已保存整书的 PDF，并下载 LaTeX、PDF 和 JSON | 草稿预览不保存内容；编译按源码 SHA-256 缓存、每书串行；修改内容、排版、页序或装订选项后旧预览失效，错误可见 |

启动器以 `backend/requirements.txt` 哈希或必要模块缺失（含 `mistune`）触发 `.venv` 中的 pip 安装，以 `frontend/package.json` 与 `pnpm-lock.yaml` 指纹或 Vite 缺失触发非交互的 `pnpm install --frozen-lockfile`。指纹只在安装成功后保存，依赖齐全且指纹未变时复用。LaTeX 准备读取当前进程编译器配置，未设置时取用户级值，选定路径传给后端；未找到编译器才下载 TinyTeX，任一准备失败均阻止服务启动。手动后端 / 直接 API 只自动查找编译器，不执行安装，详细规则见 [编译环境](LATEX_LAYOUT.md#编译环境)。

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
普通页面代理 ──► content：返回初稿 body_latex、纯文本页眉页脚
       │                      │
       │                      ▼
       │          非空正文：独立字重复核同图 + 原 LaTeX
       │                      │
       │                      ▼
       │          精确定位 spans，只插入有界样式并校验
       │
       └───────► front_cover / back_cover：结束普通转录
                              │
                              ▼
                 SpecialPageAgent：独立读取同图核心书目
                              │
                              ▼
整页成功保存 ──► Page 类型、书目、正文、页眉页脚 + 各阶段 Usage / attempts
       │
       ▼
自动填入校对栏 ► LaTeX 正文片段或纯文本书目
       │
       ▼
人工校对 ───────► PUT /api/books/{id}/pages/{number}
       │
       ├─────────► JSON
       └─────────► 整书 LaTeX 模板 ─► .tex 下载
                              │
                              ▼
                 本机 XeLaTeX ─► 缓存 PDF ─► 前端预览 / 下载

本页未保存草稿 ─► POST /api/books/{id}/pages/{number}/compile
                       └─────► 同一模板编译本页 PDF；不写页面结果
```

页面代理每次提供文件名、页码、总页数、本页图片和转录指令；复用历史仅供排版与符号参考，只输出当前页，不复制历史正文或补写跨页内容。页面中的命令都属于待转录资料，不改变任务。代理先按当前图像的排版角色判断 `page_kind`：`content`、`front_cover` 或 `back_cover`，不能因为第一页、最后一页、文件名或相邻页信息而认定封面封底。`front_cover` 包括正面外封面及以全书书名、署名、出版社等为视觉主体且无连续正文的独立书名页、内封、扉页；黑白、纯文字、馆藏章或缺少封皮边缘不使其降为内容页。章节标题页、版权页、目录及不确定页面仍是 `content`。只处理原书排印内容；可辨的印刷书法体和艺术字核心书目保留，后加批注、签名、馆藏章和馆藏编号排除。书名中的册卷信息随 `title` 保留，独立册次、卷号或版次使用既有 `edition`，不新增类别或重复收录。

普通页面代理按 `PAGE_RESPONSE_SCHEMA` 返回五个必填字段：`page_kind`、`page_side`、`header_segments`、`body_latex`、`footer_segments`。内容页在该请求中完成转录初稿；特殊类型返回 `front_cover` 或 `back_cover`、`page_side: unknown`，正文为 `""`、页眉页脚为 `[]`，不提取书目，也不继续读取其他文字。后端收到分流标记后立即调用独立 `SpecialPageAgent`，向新的客户端提供同一张本页图像、来源信息和分流类型。它采用 `SPECIAL_PAGE_AGENT_PROMPT` 与 `SPECIAL_PAGE_RESPONSE_SCHEMA`，只返回必填的 `page_kind`（`front_cover|back_cover`）和 `cover_fields`。各阶段均使用严格 JSON Schema；请求仍为非流式，分流发生在首个响应返回后，不承诺流式实时中断。

`content` 初稿的 `body_latex.strip()` 非空时，`PageAgent` 在返回结果和保存之前调用一次独立字重复核。输入为 `EMPHASIS_AGENT_PROMPT`、当前页来源信息、原样的 `body_latex` 和同一张 PNG，不重新渲染或使用预览缩略图。复核沿用协议、模型、地址、推理程度及 usage/attempts 回调，采用 `EMPHASIS_RESPONSE_SCHEMA`，只返回 `{spans:[{fragment,occurrence,style}]}`；各字段必填，`occurrence` 为正整数，`style` 仅为 `bold|boldsymbol|pmb`。`spans:[]` 表示没有新增字重。已有样式保留，不返回整页改写；封面封底及真正空正文不执行字重复核，页眉页脚仍由初稿负责。

`backend/emphasis.py` 在原始正文中从左到右按非重叠方式找每个 `fragment` 的第 `occurrence` 次精确匹配，所有范围均先定位，再统一按原始位置插入样式。不存在指定匹配、重复指定同一范围或范围交叉时直接报错；分离、相邻、完整包含的范围允许，共享起点时外层先打开，共享终点时内层先闭合。普通字重使用 `{\bfseries\boldmath 原片段}`，可包含连续段落及完整公式，不添加 `\par`；局部数学字重使用 `\boldsymbol{原片段}`，保留花体等不能直接换粗字体的字形可使用 `\pmb{原片段}`，并可嵌套在整段字重组内。每个片段调用 `validate_latex_fragment` 检查命令及环境，再检查控制词和转义字符未被截断、花括号在片段内部完整配对、片段所含数学定界符完整配对；注释、`verb`、`verbatim` 字面区域禁止接触。若结束边界后跳过空白即出现 `{` 或 `[`，而片段以控制词（含可选 `*`）或闭合参数组结束，则保守拒绝在可见参数链中插入包装，不自动扩大范围。复核提示继续要求完整的命令参数及结构边界，此模块不做通用 TeX 重写。插入后重新校验 `StructuredPageResult` 的长度和结构，并调用 `validate_latex_fragment` 校验支持词表。

特殊子代理按 `ResponsesConfig` 或 `GeminiConfig` 创建同协议独立客户端，固定 `context_reuse_enabled=False`，两种客户端均提供 `request_special_page`。Responses 专用请求发送 `store:false`，不传 `previous_response_id`；Gemini 专用请求的 `contents` 只含当前页，不携带普通代理历史。两者都不携带相邻页或第一阶段转录文本，专用响应不写入普通代理历史；协议、模型、接入地址和推理程度沿用同一任务设置，不新增模型选项或代理框架。`cover_fields` 为按原页阅读顺序排列的 `{kind,text}` 数组；`kind` 只接受 `title`、`subtitle`、`author`、`translator`、`editor`、`publisher`、`series`、`edition`、`publication_year`、`isbn`，`text` 为纯文本。只填本页可见的核心书目，排除简介、宣传、推荐语、定价、联系方式、网址等其他内容，不从上下文或常识补全；没有可辨核心信息的封底可保留空书目数组。

后端将专用提取结果合并为 Page 结构，封面封底的正文与页眉页脚为空，`page_side` 为 `unknown`，内容页的 `cover_fields` 为空数组。中间分流结果和正文初稿不落库，也不将第一阶段计作页面完成；整页成功后才保存最终结果，书目自动填入既有校对栏，无须复制；PDF 按页面类型及已保存纸型排版，需要用户主动更新。特殊阶段或字重复核失败均保留旧结果，并沿既有路径显示页面失败。非空正文页通常两次请求，空正文页一次，特殊页两次；各阶段及各自重试的实际 usage 和 attempts 累计在同一页。字重复核增加一次同图视觉输入、已有正文及复核提示的输入成本，输出仅为新增范围；费用和耗时取决于所选模型、页长及重试，不保证费用降低。暂停等待已开始的整页（包括字重复核或专用提取）结束。

当前 PDF 识别图以 `scale=2.0` 渲染（通常为 144 dpi），超过像素上限时缩小，已有识别图直接复用；PNG/JPEG 导入保留像素尺寸。复核使用相同识别图，不改变分辨率，Responses 仍设置 `detail:high`，Gemini 仍发送原 PNG。字重能否可靠辨认受原始扫描清晰度和模型影响。本轮字重复核仅完成静态阅读、编辑和审查，未运行测试、构建、类型检查、试编译、模型请求、服务启动或运行验证，不能视作识别质量或 PDF 渲染已经验证。

内容页按原页阅读顺序把页眉、正文、页脚分开，脚注和图注留在正文；不可读的原文局部写 `[无法辨认]`，空白内容页返回空语段和空正文。

整书预览的装订选项默认关闭；前端把临时 `printVersion` 通过 `print_version` 查询参数传给 LaTeX 导出与 PDF 编译接口。正文页须有非空页脚且页侧已知才使用固定源页侧别的装订边距，长正文续页沿用该源页版式；未知页侧、无非空页脚及封面封底居中，不插入凑左右面的空白页。该选项不修改识别数据或项目设置，六纸型规则见 [左右页与打印版](PRINT_LAYOUT.md)。

每条页眉或页脚语段包含必填的 `kind`、`text`、`alignment`、`row`、`font_size`、`bold`、`italic`。`alignment` 为 `left|center|right`，表示相对于整页可排印宽度的锚点，不按语段数量重新平均分列。`row` 为各自区域内 1–10 的绝对行号，同一视觉行共用行号，缺失行保留为空白，不能压缩成连续索引；字号为 `small|normal`，粗体和斜体分别作用于每条语段。旧保存结果缺少格式字段时使用居中、第一行、小号字、非粗体、非斜体的默认值。只有正文使用 LaTeX；行内公式使用 `\(...\)`，独立公式使用 `\[...\]` 或无编号数学环境。模型 `body_latex` 映射为 API `text`，其余字段保留名称并分别持久化。正文不含导言区，标题使用带 `*` 的命令，原书编号保留在文字中；脚注标号用 `\textsuperscript`，注释留在正文，不生成新编号。

`Page` 固定包含 `number`、`source_id`、`source_filename`、`source_page`、`status`、`error`、`page_kind`、`page_side`、`cover_fields`、`text`、`header_segments`、`footer_segments`、`usage` 和 `attempts`。`text` 仅为 LaTeX 正文片段，书目与页眉页脚为纯文本结构。`number` 是项目内永久页面 ID，排序不重编号；原文件页码用 `source_page` 表示。旧页面类型与页侧迁移分别使用 `content`、空书目及 `unknown`，不推断旧页侧、不重新识别。模型页侧规则及打印用途见 [左右页与打印版](PRINT_LAYOUT.md)。`Usage` 为 `{input_tokens, output_tokens, total_tokens, complete}`：供应商没有返回的字段保持 `null`，不会估算或显示为零；一次或多次重试只累加已知值，任意尝试缺少用量或无法确认消耗时 `complete` 为 `false`。`Book.usage` 是项目全部已尝试页面的合计。

`Book.content_format` 固定为 `latex`。启动时，`backend/storage.py` 在写库前用 SQLite backup 保存数据目录中的 `app-before-latex.db`，再在事务内一次性转换旧 Markdown 正文，并把原文存入 `pages.legacy_markdown` 供恢复。转换保留人工校对、状态、用量与页序，不调用模型；内部原文列不对 API 或导出公开。`mistune>=3,<4` 仅用于这次旧数据转换，产品不继续提供 Markdown 编辑、预览或导出。数据库备份可能含凭据，不可作为公开包。迁移规则及限制见 [LaTeX 排版说明](LATEX_LAYOUT.md)。

`Book` 复用为项目，`file_count` 表示来源文件数，`upload_confirmed` 和 `selection_confirmed` 分别表示上传与编排确认。`page_count` 为全部来源文件的总页数，`completed_pages` 只统计已选页中的完成数，进度分母使用 `selected_page_count`。`SourceFile` 包含 `id`、`filename`、`kind:pdf|image`、`page_count`、`position` 和 `parent_id:string|null`；`null` 表示根级文件，`position` 仍为项目内全局文件顺序。`BookDetail` 返回 `{book,files,pages}`，`pages` 是按最终顺序排列的已选页；`Arrangement` 另提供全部来源页及 `order`（已选永久 ID 的有序数组）。不可用文件层级、`Page.number` 或 `source_page` 重新排序导出，`page_order` 始终是最终页面顺序的唯一依据。

`Book.paper_size` 为 `a4|a5|a6|b5|b6|trade_6x9`，默认 `a4`，项目列表、详情及 JSON 导出均携带该字段。尺寸依次为 210 × 297、148 × 210、105 × 148、176 × 250、125 × 176、152.4 × 228.6 毫米；B5、B6 使用 ISO 尺寸。SQLite 增量添加该列，旧项目采用 A4，保留原页面、状态和用量。纸型用于整书重排，不改变源图比例或 OCR 输入。

`Book.layout` 保存整书 `LayoutSettings`：`font_family: songti|heiti|kaiti`（默认宋体）、`font_size_pt: number|null`（默认纸型字号）、`line_height: number`（默认 1.6）、`paragraph_indent: number`（默认 2 汉字）、`paragraph_spacing_pt: number`（默认 0）、`margin_mm: number|null`（默认纸型边距）。前端的 pt 在 LaTeX 中使用 bp，基础基线距离为字号乘以行距倍率。项目列表、详情和 JSON 均携带排版设置；它们不改变 OCR 输入或页序。

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
| PUT | `/api/books/{id}/layout` | 保存 `{paper_size?: PaperSize, layout?: LayoutSettings}`，至少一项；前端提交完整排版设置，返回 `Book`，不改变正文或处理状态，不调用模型 |
| POST | `/api/books/{id}/process` | 新项目必须已确认上传与编排；可提交已选 ID 子集 `{"pages":[3,1]}`，实际按编排顺序处理；省略 `pages` 时处理全部已选页（包括 `ready`）；运行中返回 409。旧单文件接口保留首次选择兼容 |
| POST | `/api/books/{id}/pages` | 兼容接口，`{"pages":[StrictInt,...]}` 按永久页面 ID 追加到清单末尾，返回 `BookDetail`；首次确认前或越界添加返回 400 |
| DELETE | `/api/books/{id}/pages/{number}` | 单页移出清单，返回 `BookDetail`；首次确认前返回 400，当前 `processing` 页返回 409；允许移除最后一页 |
| POST | `/api/books/{id}/pause` | 请求暂停；不再启动新页，所有已开始页结束后暂停，未运行时返回 409 |
| DELETE | `/api/books/{id}` | 删除单本书及其页面、请求用量记录和文件；运行中返回 409 |
| PUT | `/api/books/{id}/pages/{number}` | 保存校对结果：`text` 必填，`page_kind`、`cover_fields` 可选；旧 `{text}` 请求兼容 |
| GET | `/api/books/{id}/export` | 确认编排后下载按最终顺序排列的 `{book,files,pages}` 结构化 JSON |
| GET | `/api/books/{id}/export.tex?print_version=false` | 确认编排后下载按最终页序生成的完整 LaTeX 文档；不调用编译器 |
| POST | `/api/books/{id}/compile?print_version=false` | 编译已保存整书，返回 `{pdf_url, warnings:string[]}`；同源码复用缓存，每书串行 |
| POST | `/api/books/{id}/pages/{number}/compile?print_version=false` | 请求体为 `PageUpdate` 草稿，只编译本页并返回 `{pdf_url, warnings:string[]}`；不保存校对或用量 |
| GET | `/api/books/{id}/compiled/{sha256}.pdf` | 内联返回成功生成的 PDF，供预览与下载 |
| GET | `/api/books/{id}/assets/{name}` | 安全范围内读取已保存页面文件 |

所有响应字段都应保留契约约定的 nullable 字段。错误使用 FastAPI 的 `detail` 文本。前端通过轮询观察状态，不引入 WebSocket。

排版更新至少提供 `paper_size` 或 `layout`，提交的字段不能为 `null`；`layout.font_size_pt`、`layout.margin_mm` 可为 `null`，表示采用纸型默认值。字段范围见 [排版设置](LATEX_LAYOUT.md#整书排版设置)。非法值返回 `422`，不存在的项目返回 `404`。该接口独立于单页校对和编排确认，不重置确认标记，不修改正文、书目、页眉页脚、用量或尝试次数。选择纸型后即保存，整书排版表单需点击保存；修改后需主动更新 PDF。

页面校对请求的可选 `page_kind`、`cover_fields` 可以省略，不能显式为 `null`。内容页不接受非空书目；封面封底不接受非空 `text`，保存时清空页眉页脚并将 `page_side` 清为 `unknown`。切回内容页时清空书目，正文由本次 `text` 提供，页侧仍为 `unknown`；正文校对保留已有页侧，不开放页侧编辑字段。省略类型时沿用已保存类型，省略书目时封面封底保留已有书目；保存不改变 `usage` 或 `attempts`。前端提供页面类型纠正及封面书目增删和文本编辑，仅当前页为 `processing` 时拒绝保存；其他页在整书 `processing` 或 `pausing` 时仍可校对，保存保留整书任务状态。等待识别的页面若成功人工保存，本轮任务跳过该页（包括已被上下文复用组预留的页），防止覆盖校对；后续主动发起的新识别任务不受影响。

新界面统一在页面编排中维护选页和顺序；PDF 和图片在同一文件树中排序，类型仅作标识。文件排序立即反映到最终页面草稿；嵌入文件在父文件下显示为子级，移动文件时连同子文件整组移动并保留已有选页范围。根文件携带子文件移入另一根文件时，整组成员都成为目标根文件的直接子文件，不增加层级；子文件不能作为嵌入目标。文件内视图展示当前父文件与全部子文件页面，局部页面排序只置换当前文件组占用的成书位置，保留组外页面位置。嵌入默认保持已编入范围；来源文件尚未编入页面时，由用户明确选择是否编入全部页，不隐式扩大范围。页码范围按当前来源文件从 1 开始计数，不使用印刷页码；范围解析可以去重升序，但最终页面顺序以用户编排为准，不在保存或识别时重新升序。页面预览按需请求，网格不一次性渲染整份 PDF。

旧页面增删 API 保留兼容：运行中的流程通过共享 `pending_pages` 消费队列，追加未完成页可入队，移出的未开始页跳过，当前识别页不可移除。新界面在闲置时通过编排统一修改，不在处理期间重排。“识别未完成页”由前端明确提交未完成页；全部完成时隐藏，不提供整批重新识别，当前页可单独重新识别。成功替换旧文本，失败保留旧文本，`attempts` 和已知 `usage` 累计。未保存草稿需先保存或处理提示。项目保持 `processing` 或 `pausing` 直到本次处理结束；暂停时不再启动新页，等待所有已开始页结束后进入 `paused`，保留结果和用量，已暂停状态在重启后保留。新任务（含暂停后继续）重新分组，不复用旧响应 ID。

单页 PDF、整书 PDF 和 LaTeX 下载使用同一后端模板：封面按标题、署名和出版信息分层，封底书目靠底，内容页使用页眉、正文、页脚结构。纸型改变纸张尺寸、页边距及可排内容宽度，封面字号和留白随纸型适配；整书排版决定字体、字号、基础行距、缩进与段距。页眉页脚按整页 `alignment` 锚点、`row` 绝对行号及字形排版，保留缺失行的空白；内容页可自然续页。项目纸型和排版保存在后端，不在 `localStorage` 保存。来源信息保留在界面及 JSON，不加书内标题、目录、自动编号或额外页码。JSON 保留 LaTeX 正文与完整结构，当前没有 Markdown 或 HTML 下载接口。

PDF 编译使用 XeLaTeX、`ctexbook` 与 Fandol，一键启动先准备编译环境，手动后端则要求用户自行备齐；`EBOOK_OCR_XELATEX` 只指定可执行文件。`build_latex(detail, print_version=False)` 返回完整源码，`async compile_pdf(source, output_dir)` 返回 PDF 路径。API 按源码 SHA-256 在每书 `latex-cache` 目录缓存，以每书锁串行编译。调用不经过 shell，禁用 shell-escape，Windows 进程隐藏，并设置超时。前端主动生成 PDF，单页请求可预览未保存草稿；内容、排版、页序或装订变更使旧预览失效，失败信息可见。本轮一键依赖准备仅作静态阅读、编辑与审查，未执行安装、测试、构建、类型检查、文档编译、真实识别或服务启动/重启，不能将历史验收记录当作当前版本已验证。

设置中的 `processing_concurrency` 为正整数，默认 10，无固定上限；调度有界，每个项目同时处理页数不超过配置，多个项目各自限制。`context_reuse_enabled` 为布尔值，默认 `false`；`context_reuse_max_pages` 为 1–10 的整数，默认 10，含首张。旧设置缺字段使用默认值。保存设置不联网，每次任务启动时固定设置快照，修改下次任务生效。开启实验性复用后，按本次待处理页的最终编排顺序连续分组，组内串行、不同组并行。每组普通代理使用独立的所选协议客户端：OpenAI 发送 `store:true`，以 `previous_response_id` 续接成功响应；Gemini 在本地保存完整 `contents`（图片和原始模型 `thoughtSignature` 一并保留）并随下一次请求发送，不使用服务端 `cachedContent`。特殊子代理和字重复核始终各自使用另一个同协议独立客户端并关闭复用：Responses 为 `store:false` 且无父响应 ID，Gemini 的 `contents` 只含当前页；专用响应不接入普通代理历史链，字重复核也不将初稿或新增范围写回普通历史。默认普通代理使用独立上下文，OpenAI 为 `store:false`，Gemini 只含当前页。OpenAI 兼容服务须支持保存与续接，缺少响应 ID 时报错，不静默降级；两种协议页面失败均保留旧结果并重置对话，下一页从新上下文开始。历史仍占用上下文与用量，不保证费用降低。完成可能乱序，呈现与导出仍遵循编排。

设置中的 `reasoning_effort` 是可选字符串，默认空值使用服务默认。OpenAI 非空时传递 `reasoning: {"effort": value}`，不限制枚举；Gemini 将 `minimal/low/medium/high` 映射为 `thinkingConfig.thinkingLevel`（Gemini 3），将不小于 `-1` 的整数映射为 `thinkingConfig.thinkingBudget`（Gemini 2.5；`-1` 动态，`0` 关闭是否可用取决于模型）。客户端不按模型名猜测参数。识别和连接测试均不发送最大输出 token 设置（OpenAI `max_output_tokens`、Gemini `maxOutputTokens`），旧保存值不生效；额度遵循服务默认和模型限制。截断时提示检查输出限制或降低推理程度，已返回 usage 仍计入。删除整本书接口成功返回 `204 No Content`；未知书籍返回 `404`，正在处理的同一本书返回 `409`。删除只作用于目标书籍，不改变其他书籍、全局设置或 API 密钥。

设置默认 `api_protocol=openai_responses`、`base_url=https://api.openai.com/v1`、`responses_path=/responses`、`models_path=/models`；旧设置缺协议时沿用 OpenAI，缺模型路径时采用 `/models`。Gemini 协议为 `gemini`，官方根地址 `https://generativelanguage.googleapis.com/v1beta`、模型集合路径 `/models`，无具体模型预设。切换协议不联网，默认服务地址可随协议替换，自定义地址保留。OpenAI 两个相对路径留空时直连根地址；Gemini 不使用 `responses_path`，`models_path` 表示资源集合，空值表示根地址自身就是集合。生成地址为集合加 `/`、编码后的短模型名及 `:generateContent`，手动模型 ID 接受裸名称或 `models/` 前缀。

`POST /api/settings/models` 请求体为 `{api_protocol?:"openai_responses"|"gemini",base_url:string,models_path:string,api_key?:string,clear_api_key?:boolean,timeout_seconds:number}`。前端提交当前草稿，无须先保存；OpenAI 以 Bearer GET 读取 `data[].id`。Gemini 以 `x-goog-api-key` GET 模型集合，跟随 `nextPageToken`（下次传 `pageToken`），仅返回 `supportedGenerationMethods` 包含 `generateContent` 的完整 `name`，结果仍为 `{models:string[]}`。空密钥仅在规范化后的根地址、协议都与已保存配置一致且未清除时复用。获取不修改设置或凭据、不发起推理、不随输入自动联网；失败可手填模型 ID，列表不证明图像或 JSON Schema 能力，敏感上游响应不透传。

`PUT /api/settings` 在有旧密钥且规范化根地址或协议变化时，要求新密钥或 `clear_api_key=true`，否则返回 `400`；没有旧密钥时允许保存空配置。同一连接身份下空白密钥可保留旧值，显式清除优先。读取设置不返回密钥。

普通页、特殊页及字重复核请求均按显式协议选择客户端。OpenAI 使用 base64 PNG `input_image` 与 `text.format` 的 `json_schema`（`strict: true`）；Gemini 使用原生 `systemInstruction`、`contents`/`inlineData` 及 `generationConfig`，通过 `responseMimeType: application/json` 和 `responseJsonSchema` 请求结构化输出，不发送 OpenAI 参数。旧 `structured_output` 字段仅用于兼容。连接测试在保存后由用户主动触发非流式文本推理，可能产生用量，不代表图像识别质量。

Gemini 输入 token 取 `usageMetadata.promptTokenCount`，输出取 `candidatesTokenCount + thoughtsTokenCount`（thoughts 缺失按 0；candidates 未知则输出未知），总量取 `totalTokenCount`，未知字段保持未知，不估算。协议参考：[Google 模型列表](https://ai.google.dev/api/models)、[Google generateContent](https://ai.google.dev/api/generate-content)。

## 安全与限制

- 默认监听 `127.0.0.1`；这是本地单用户工具，不提供账号或多用户权限。
- API 密钥按用户授权保存到 SQLite 的独立凭据记录中，本机明文存储；启动时加载，不进入前端存储、书籍导出和日志。空白/省略保留，非空替换，`clear_api_key=true` 优先清除；设置和凭据在同一事务中提交，失败时不更新内存。升级到支持持久化密钥的版本后，重启新版并重新输入保存一次旧内存 key 即可，之后不必每次重启重填。
- API 根地址限制为 `http(s)`，拒绝 userinfo、query、fragment；请求不自动重定向，避免泄漏 Authorization。
- 模型输出按不可信输入检查响应状态和文本长度；拒绝内容或 incomplete 响应应成为可见失败。
- LaTeX 正文仅允许 `backend/latex_content.py` 所列命令与环境；不允许导言区、宏定义、任意文件读写和外部资源命令。排版错误由编译接口可见报告，不自动改写正文。
- 运行中的任务重启后标记 `interrupted`；有限重试只覆盖暂时错误，不做无限重试。
- PDF 上传仅建立宽高为 0 的轻量页面记录。原页预览独立按需渲染；识别时逐页更新实际宽高并补齐所选页缺少的 PDF、PNG，单页文件保留源页尺寸、旋转和裁切，已有资产复用。上传、选择和预览均不发起模型请求。
- 不依赖 Redis、Celery、微服务、插件系统或通用工作流引擎。

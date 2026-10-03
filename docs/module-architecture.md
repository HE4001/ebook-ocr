# 架构模块说明

项目按“本地前后端分离、逐页处理、文本校对和导出”的边界组织。根目录 [ARCHITECTURE.md](../ARCHITECTURE.md) 保留既有架构记录；本文维护当前处理范围、状态和 HTTP 接口要求，并解释运行时职责。

## 模块与责任

`layout_contract.py` 定义版本1观察与程序来源，`fidelity_rendering.py` 将自然原行和公式组放入单页画布，`latex_diagnostics.py` 读取 measurement-v2 CSV 与编译日志，`compile_service.py` 检查实际 PDF、保存诊断和映射、管理单页与合并缓存。前端 `SourceComparison`、`LayoutCalibration` 分别负责同页对照及局部校准。默认识别协议为响应 v2，旧 v1 仅显式选择兼容。

每源页独立编译，单页缓存不包含当前编排位置；合并缓存包含最终顺序。缓存身份包含当前来源、内容及布局修订、渲染策略、样式、纸型、打印选择和生成器/诊断/编译器/字体版本，复用时核对 PDF 指纹。布局生成的完整源码仍按还原策略处理，只有自定义完整文档由自身源码控制。

响应 v2 仍为一次识别，增加必填 `response_version` 和布局观察；元数据由程序提供。数据库添加 `render_strategy`、来源/布局 JSON 和修订，写前保存 `app-before-layout.db`；历史页保持模板。自动结果按内容修订 CAS 保存，校准按内容/布局双修订保存，原稿归档保留。源码实际修改后转自定义，旧布局仍可恢复。

| 方法 | 新增或扩展契约 |
| --- | --- |
| `PUT /api/books/{id}/pages/{number}/layout` | 提交 `observation`、两个 expected 修订及可选画布/字体，保存布局并生成源码；冲突409 |
| `POST /api/books/{id}/pages/{number}/compile-layout-pdf` | 同样的校准请求，仅预览草稿，不保存 |
| `GET /api/books/{id}/compiled/{digest}/pages/{output_page}.png` | 按实际物理页按需输出PNG，最大边1800px |
| 既有整书/本页 `/compile` | 返回 `pdf_url`、兼容 `warnings`、`diagnostics`、`page_map`、`quality_status` 和两个版本号；失败也返回结构化状态 |
| 项目排版更新 | 可提交 `render_strategy`，排版中有 `source_fidelity_paper:project|source` |
| 自由源码保存 | 可提交两个 expected 修订和策略；旧 `{text}` 请求保留兼容 |

校准参数省略沿用当前值，画布宽高需成对提交或成对清空，显式 `null` 恢复未知；基准字体未共同确认时，未知或清空项不能标为人工确认。完整诊断包含源ID、编排/输出范围、修订、代码、严重度、依据、覆盖和可用bbox。`passed`、`needs_review`、`unverified`、`compile_failed` 必须区别；缺测量不能因未报错变成通过。

| 模块 | 责任 | 关键边界 |
| --- | --- | --- |
| `ocr.bat` / `scripts/start.ps1` | Windows 一键入口；真实启动前准备 Python、前端与 LaTeX 依赖，再管理本地服务 | Python / Node.js / pnpm 仍是前置条件；后端依赖仅安装到 `.venv`；按需求文件指纹和缺失模块 / Vite 决定安装，成功后缓存指纹；已有正常服务直接返回，不改其依赖 |
| `scripts/latex-dependencies.ps1` | 公开 `Initialize-LatexDependencies`；查找 XeLaTeX，必要时下载并校验官方 TinyTeX-1，补齐模板依赖 | 显式配置无效时警告后自动查找；已有无效安装移为同级备份再安装；依赖齐全不联网，每次准备编译固定探针，不读取用户书稿；只修改启动进程环境。详细限制见 LaTeX 编译环境说明 |
| `frontend/` | React + TypeScript + Vite 界面；项目、多文件上传确认、页面编排、轮询、文本校对、预览、导出和项目删除 | 上传确认及编排确认完成后才开放校对与整书预览；不保存 API 密钥到浏览器 |
| `frontend/src/pageSelection.ts` | 将单数页、偶数页或自定义页码解析为明确的页码列表 | 源文件从 1 计数；校验范围端点后展开，去重并升序；任一错误返回空列表和中文错误 |
| HTTP API | FastAPI 路由、请求校验、状态和错误文本 | 所有接口使用 `/api` 前缀；ID、页码和路径必须验证 |
| 导入与页面文件 | 一个项目包含多个来源文件，按 PDF / 图片分类；PDF 上传仅读取页数、保存源文件和轻量页面记录；内容预览独立按需缓存，识别时准备单页 PDF 与 PNG | 来源存入各自目录，避免同名冲突；预览最大边 1200 px；每个文件限制 100 MB，保留像素限制 |
| 持久化 | SQLite 保存设置、独立凭据、书籍纸型与排版、LaTeX 正文、结构化书目及页眉页脚、用量和尝试次数；文件系统保存原页资产与编译缓存 | 默认目录为 `backend/data/`，可用 `EBOOK_OCR_DATA_DIR` 覆盖；凭据记录在 SQLite 中本机明文保存；旧正文仅启动时迁移，写前备份；删除书籍时一并删除其记录和文件 |
| 协议客户端 | 按 `api_protocol` 使用 OpenAI Responses（Bearer）或 Gemini 原生 `generateContent`（`x-goog-api-key`），读取各自 usage | 非流式 POST，不自动切换 Chat Completions或跟随重定向；Gemini 仅 API Key 开发者 API，无 Vertex OAuth 或多账户系统 |
| 模型列表 | 当前草稿 GET `base_url + models_path`；OpenAI 读取 `data[].id`，Gemini 分页读取支持 `generateContent` 的完整 `models/...` 名称 | 不保存草稿或密钥，不发起推理，不随输入自动请求；列表不证明图像或结构化输出能力 |
| 页面代理 | 同一次请求判断页面类型，保存内容页原行布局或封面封底书目 | 默认 v2 八字段 Schema，v1 显式兼容；原行与公式组关系不因纸型重排；模型观察与程序元数据分开，未知保持未知；默认独立上下文，历史仅作字形与符号参考 |
| 流程协调 | 上传与编排分别确认，持久化文件层级、文件顺序及最终页面顺序；按项目有界并发，累计尝试和已知用量 | 编排草稿确认后保存；处理或上传期间禁止重排；暂停不再启动新页，等待已开始页结束；重跑成功替换旧文本、失败保留旧文本 |
| `backend/latex_content.py` | 纯文本转义、识别正文片段或完整文档的源码格式 | `Page.text` 可为正文片段或以 `\documentclass` 开头的完整文档；无命令或环境白名单；书目与页眉页脚仍为纯文本 |
| `backend/latex_export.py` | 按编排为每源页生成独立源码单元 | 还原页由布局生成；片段与书目使用单页模板；完整自定义文档保留源码和全部输出页，不应用项目布局或装订 |
| PDF 预览与导出 | 主动编译草稿或已保存内容，下载 LaTeX、PDF、JSON | 草稿不保存；每书串行，独立单页缓存与顺序相关合并缓存分开；诊断、质量、范围和映射随修订返回，旧结果失效后不能冒充最新 |

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
一次页面请求 ──► v2 八字段：类型、兼容正文/语段、书目、布局观察
       │          content：原行/区域/公式组布局、空 cover_fields
       │          front_cover / back_cover：可见书目、layout:null、空正文/语段
       ▼
整页成功保存 ──► 内容修订 CAS；原行、生成源码、兼容字段 + Usage / attempts
       │
       ▼
自动填入校对栏 ► LaTeX 片段 / 完整文档或纯文本书目
       │
       ▼
人工校对 ───────► 双修订校准生成源码 / 自由源码保存后自定义
       │
       ├─────────► JSON
       └─────────► 逐源页源码单元 ─► 单份 .tex / 多份 .zip 下载
                              │
                              ▼
                 本机 XeLaTeX ─► 单元诊断/实际范围 ─► 按顺序合并及页映射

本页未保存草稿 ─► POST /api/books/{id}/pages/{number}/compile
                       └─────► 按当前草稿策略编译；不写页面结果
```

页面代理每次提供文件名、页码、总页数、本页图片和转录指令；复用历史仅供字形与符号参考，不能改变本页行序或断行，只输出当前页，不复制历史正文或补写跨页内容。页面中的命令都属于待转录资料，不改变任务。代理先按当前图像的排版角色判断 `page_kind`：`content`、`front_cover` 或 `back_cover`，不能因为第一页、最后一页、文件名或相邻页信息而认定封面封底。`front_cover` 包括正面外封面及以全书书名、署名、出版社等为视觉主体且无连续正文的独立书名页、内封、扉页；黑白、纯文字、馆藏章或缺少封皮边缘不使其降为内容页。章节标题页、版权页、目录及不确定页面仍是 `content`。只处理原书排印内容；可辨的印刷书法体和艺术字核心书目保留，后加批注、签名、馆藏章和馆藏编号排除。书名中的册卷信息随 `title` 保留，独立册次、卷号或版次使用既有 `edition`，不新增类别或重复收录。

`PageAgent` 使用统一提示词和所选版本 Schema，一次非流式请求完成页面分类及识别。默认 v2 的八字段全部必填：`response_version:2`、`page_kind`、`page_side`、`header_segments`、`body_latex`、`footer_segments`、`cover_fields`、`layout`。内容页 `layout` 必须提供，空白页是空观察；封面封底 `layout:null`，正文/语段为空，页侧未知。v1 六字段仅显式选择，不能作为默认协议描述。

首次识别逐行辨认文字字族、粗体、斜体及混排数学的可见样式。黑体字族使用有界的 `{\heiti 原文}`，粗体文字使用 `\textbf{原文}`；原页同时为黑体和粗体时可叠加，二者不互相推定，不按标题、定义等语义自动加粗。文字与完整公式同时加粗使用 `{\bfseries\boldmath 原文与完整公式}`，局部数学符号按可见字形使用 `\mathbf`、`\boldsymbol` 或局部 `\pmb`。统一提示词说明 LaTeX 常用语法和两种源码形式；`body_latex` 可为片段或完整文档。后端检查响应结构和长度，不再以命令及环境白名单拒绝源码；实际语法、宏包、字体和资源错误由 XeLaTeX 报告。

`cover_fields` 为按原页阅读顺序排列的 `{kind,text}` 数组；`kind` 只接受 `title`、`subtitle`、`author`、`translator`、`editor`、`publisher`、`series`、`edition`、`publication_year`、`isbn`，`text` 为保留原断行的纯文本。只填本页可见的核心书目，排除简介、宣传、推荐语、定价、联系方式、网址等其他内容，不从上下文或常识补全；没有可辨核心信息的封底可保留空书目数组。

每个内容页、空白页、封面或封底正常识别均为一次请求，成功后保存整页结果，书目自动填入既有校对栏；PDF 按页面类型及已保存纸型排版，需要用户主动更新。现有失败重试及用户主动重新识别继续累计同页的实际 usage 和 attempts；重试不构成固定的识别第二遍。失败保留旧结果，暂停等待已开始页面完成后结束。

当前 PDF 识别图以 `scale=2.0` 渲染（通常为 144 dpi），超过像素上限时缩小，已有识别图直接复用；PNG/JPEG 导入保留像素尺寸。Responses 设置 `detail:high`，Gemini 发送原 PNG。字族和字重辨认受扫描和模型影响。当前测试、真实编译及 UI 证据见[修复验收报告](RENDERING_REPAIR_VERIFICATION.md)；固定响应与人工校准不证明真实模型识别精度。

内容页按原页阅读顺序把页眉、正文、页脚分开，脚注和图注留在正文；不可读的原文局部写 `[无法辨认]`，空白内容页返回空语段和空正文。

v2 的 `layout.lines` 独立保存原行内容、顺序、区域、归一化 bbox、基线和样式；文字行包含完整行内数学，公式行仅含数学体。公式组单独保存行序、共同锚点及编号。还原布局是内容权威，生成器按自然盒和绝对基线放置，再统一映射完整源画布。模型观察只能是估计或未知，来源指纹、物理尺寸、变换及修订由程序补入。兼容片段和自定义源码仍可使用固定行盒，但不能据此声称已有原图几何。

还原预览和导出从当前结构生成，`Page.text` 是兼容生成源码；原样保存仍由布局控制。自由源码实际修改后切为 `custom_latex`，`generated_content_revision` 清空，旧布局保留但不覆盖人工稿。旧页无需重新识别即可校准；不能从旧字符串猜出原图断行。自动结果按内容修订 CAS 保存，校准按双修订保存，旧模型响应拒绝后不推进复用上下文。草稿编译不保存页面结果。

装订选项默认关闭，临时 `printVersion` 传至源码导出和编译接口，不持久化。项目纸型还原正文页按已知页侧移动留白，原页尺寸不另加；模板/自定义片段需非空页脚且页侧已知，完整自定义文档由自身源码控制。续页沿用源页侧别，不按输出奇偶插空白页，详见 [打印说明](PRINT_LAYOUT.md)。

兼容页眉页脚语段包含 `kind`、`text`、`alignment`、`row`、`font_size`、`bold`、`italic`。左/中/右锚点相对整页可排印宽度，`row` 为各区域内1—10的绝对行号，缺行保留空白，字号 `small|normal`，各语段字形独立。旧结果缺字段采用居中、第一行、小号字、非粗体/斜体。模板/自定义页保存 `body_latex` 源码，还原页的 `text` 由布局生成；完整自定义文档的兼容语段只作元数据，实际由源码渲染。识别不新增编号或自动改写人工稿。

`Page` 包含永久来源身份、状态、页面类型、正文/书目/兼容语段、用量和次数，以及策略、内容/布局修订、生成修订、`layout_source` 与 `source_metadata`。`number` 排序不重编号，原文件页码用 `source_page` 表示；旧类型与页侧默认为内容页和未知，不自动重识别。页侧与打印见 [打印说明](PRINT_LAYOUT.md)。`Usage` 的未返回字段保持 `null`，重试只累加已知值，无法确认消耗时 `complete:false`；`Book.usage` 合计全部已尝试页。

`Book.content_format` 固定为 `latex`。启动时，`backend/storage.py` 在写库前用 SQLite backup 保存数据目录中的 `app-before-latex.db`，再在事务内一次性转换旧 Markdown 正文，并把原文存入 `pages.legacy_markdown` 供恢复。转换保留人工校对、状态、用量与页序，不调用模型；内部原文列不对 API 或导出公开。`mistune>=3,<4` 仅用于这次旧数据转换，产品不继续提供 Markdown 编辑、预览或导出。数据库备份可能含凭据，不可作为公开包。迁移规则及限制见 [LaTeX 排版说明](LATEX_LAYOUT.md)。

`Book` 复用为项目，`file_count` 表示来源文件数，`upload_confirmed` 和 `selection_confirmed` 分别表示上传与编排确认。`page_count` 为全部来源文件的总页数，`completed_pages` 只统计已选页中的完成数，进度分母使用 `selected_page_count`。`SourceFile` 包含 `id`、`filename`、`kind:pdf|image`、`page_count`、`position` 和 `parent_id:string|null`；`null` 表示根级文件，`position` 仍为项目内全局文件顺序。`BookDetail` 返回 `{book,files,pages}`，`pages` 是按最终顺序排列的已选页；`Arrangement` 另提供全部来源页及 `order`（已选永久 ID 的有序数组）。不可用文件层级、`Page.number` 或 `source_page` 重新排序导出，`page_order` 始终是最终页面顺序的唯一依据。

`Book.paper_size` 为 `a4|a5|a6|b5|b6|trade_6x9`，默认 `a4`，项目列表、详情及 JSON 导出均携带该字段。尺寸依次为 210 × 297、148 × 210、105 × 148、176 × 250、125 × 176、152.4 × 228.6 毫米；B5、B6 使用 ISO 尺寸。SQLite 增量添加该列，旧项目采用 A4，保留原页面、状态和用量。纸型决定模板输出尺寸，完整文档使用自身设置；不为适配纸型改写正文原行，也不改变源图比例或 OCR 输入。

`Book.layout` 保存 `source_fidelity_paper:project|source`（默认项目纸型）及模板字体、字号、行距、缩进、段距和边距。模板默认为宋体、字号和边距随纸型、行距1.6、缩进2汉字、段距0。还原按布局原行放置；项目设置用于整页映射和未知字体的临时预览，不能当作测量事实。完整自定义文档使用自身配置。前端 pt 对应 LaTeX bp；项目设置进入列表、详情和 JSON，不改变 OCR 输入或页序。

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
| PUT | `/api/books/{id}/layout` | 保存可选纸型、排版及默认策略，至少一项；返回 `Book`，不改变正文或处理状态，不调用模型 |
| POST | `/api/books/{id}/process` | 新项目必须已确认上传与编排；可提交已选 ID 子集 `{"pages":[3,1]}`，实际按编排顺序处理；省略 `pages` 时处理全部已选页（包括 `ready`）；运行中返回 409。旧单文件接口保留首次选择兼容 |
| POST | `/api/books/{id}/pages` | 兼容接口，`{"pages":[StrictInt,...]}` 按永久页面 ID 追加到清单末尾，返回 `BookDetail`；首次确认前或越界添加返回 400 |
| DELETE | `/api/books/{id}/pages/{number}` | 单页移出清单，返回 `BookDetail`；首次确认前返回 400，当前 `processing` 页返回 409；允许移除最后一页 |
| POST | `/api/books/{id}/pause` | 请求暂停；不再启动新页，所有已开始页结束后暂停，未运行时返回 409 |
| DELETE | `/api/books/{id}` | 删除单本书及其页面、请求用量记录和文件；运行中返回 409 |
| PUT | `/api/books/{id}/pages/{number}` | 保存自由源码：`text` 必填，类型、书目、策略及预期双修订可选；旧 `{text}` 请求兼容，修订冲突409 |
| GET | `/api/books/{id}/export` | 确认编排后下载按最终顺序排列的 `{book,files,pages}` 结构化 JSON |
| GET | `/api/books/{id}/export.tex?print_version=false` | 按最终页序下载源码；单份为 `.tex`，多份为含各 `.tex` 的 ZIP，ZIP 的 Content-Type 为 `application/zip`，文件扩展为 `.zip`；不调用编译器 |
| POST | `/api/books/{id}/compile?print_version=false` | 编译已保存整书，返回 PDF 链接、兼容 warnings、结构化诊断、页映射、质量和版本；每书串行 |
| POST | `/api/books/{id}/pages/{number}/compile?print_version=false` | `PageUpdate` 草稿只预览本页，返回相同结构；不保存校对或用量，修订冲突409 |
| GET | `/api/books/{id}/compiled/{sha256}.pdf` | 内联返回成功生成的 PDF，供预览与下载 |
| GET | `/api/books/{id}/assets/{name}` | 安全范围内读取已保存页面文件 |

所有响应字段都应保留契约约定的 nullable 字段。错误使用 FastAPI 的 `detail` 文本。前端通过轮询观察状态，不引入 WebSocket。

排版更新至少提供 `paper_size`、`layout` 或 `render_strategy` 一项，提交字段不能为 `null`；`layout.font_size_pt`、`layout.margin_mm` 可为 `null`，表示纸型默认值。默认策略变化只更新仍沿用原项目策略的页面，保留已脱离的页策略。非法值422，未知项目404；不重置确认标记或内容/用量。选择纸型即保存，整书表单需点击保存；修改后主动更新 PDF。

页面校对请求的可选 `page_kind`、`cover_fields` 可以省略，不能显式为 `null`。内容页不接受非空书目；封面封底不接受非空 `text`，保存时清空页眉页脚并将 `page_side` 清为 `unknown`。切回内容页时清空书目，正文由本次 `text` 提供，页侧仍为 `unknown`；正文校对保留已有页侧，不开放页侧编辑字段。省略类型时沿用已保存类型，省略书目时封面封底保留已有书目；保存不改变 `usage` 或 `attempts`。前端提供页面类型纠正及封面书目增删和文本编辑，仅当前页为 `processing` 时拒绝保存；其他页在整书 `processing` 或 `pausing` 时仍可校对，保存保留整书任务状态。等待识别的页面若成功人工保存，本轮任务跳过该页（包括已被上下文复用组预留的页），防止覆盖校对；后续主动发起的新识别任务不受影响。

新界面统一在页面编排中维护选页和顺序；PDF 和图片在同一文件树中排序，类型仅作标识。文件排序立即反映到最终页面草稿；嵌入文件在父文件下显示为子级，移动文件时连同子文件整组移动并保留已有选页范围。根文件携带子文件移入另一根文件时，整组成员都成为目标根文件的直接子文件，不增加层级；子文件不能作为嵌入目标。文件内视图展示当前父文件与全部子文件页面，局部页面排序只置换当前文件组占用的成书位置，保留组外页面位置。嵌入默认保持已编入范围；来源文件尚未编入页面时，由用户明确选择是否编入全部页，不隐式扩大范围。页码范围按当前来源文件从 1 开始计数，不使用印刷页码；范围解析可以去重升序，但最终页面顺序以用户编排为准，不在保存或识别时重新升序。页面预览按需请求，网格不一次性渲染整份 PDF。

旧页面增删 API 保留兼容：运行中的流程通过共享 `pending_pages` 消费队列，追加未完成页可入队，移出的未开始页跳过，当前识别页不可移除。新界面在闲置时通过编排统一修改，不在处理期间重排。“识别未完成页”由前端明确提交未完成页；全部完成时隐藏，不提供整批重新识别，当前页可单独重新识别。成功替换旧文本，失败保留旧文本，`attempts` 和已知 `usage` 累计。未保存草稿需先保存或处理提示。项目保持 `processing` 或 `pausing` 直到本次处理结束；暂停时不再启动新页，等待所有已开始页结束后进入 `paused`，保留结果和用量，已暂停状态在重启后保留。新任务（含暂停后继续）重新分组，不复用旧响应 ID。

单页 PDF、整书 PDF 与 LaTeX 均逐源页生成单元。还原页使用布局；片段与封面封底使用单页模板，书目及语段顺序和样式保留；完整自定义文档不套模板或项目设置，可输出多页。还原和模板异常续页会诊断，完整文档的结构化字段仅为元数据。项目设置、完整源码与布局进入 JSON，当前没有 Markdown 或 HTML 下载接口。

PDF 使用本机 XeLaTeX，单元缓存为 `latex-units`，整书为 `latex-cache`，复用时核对 PDF 指纹。稳定 `page_map` 保存来源、编排位置、实际输出范围、修订、策略及可用变换。源码导出单份 `.tex`，多份 ZIP 含编号文件与编排映射；前端按 Content-Type 以 Blob 下载。编译不经过 shell，禁用 shell-escape，Windows 隐藏且设超时，不自动安装任意源码依赖或上传资源。当前实测与验收结论见修复验收报告，浏览器下载保存尚未取得文件路径证据。

`warnings` 继续为兼容字符串数组，质量由结构化诊断决定。日志、自然盒与实际 PDF 字形范围互补；零宽固定盒没有 Overfull 仍可报纸外内容。非零 glyph 的 U+FFFD 表示 Unicode 映射未知，以 info/partial 及未验证保留，不能伪称缺字或通过。有诊断的 PDF 草稿可查看下载，内容/版式通过须另核对。

设置中的 `processing_concurrency` 为正整数，默认 10，无固定上限；调度有界，每个项目同时处理页数不超过配置，多个项目各自限制。`context_reuse_enabled` 为布尔值，默认 `false`；`context_reuse_max_pages` 为 1–10 的整数，默认 10，含首张。旧设置缺字段使用默认值。保存设置不联网，每次任务启动时固定设置快照，修改下次任务生效。开启实验性复用后，按本次待处理页的最终编排顺序连续分组，组内串行、不同组并行。每组页面代理使用独立的所选协议客户端：OpenAI 发送 `store:true`，以 `previous_response_id` 续接成功响应；Gemini 在本地保存完整 `contents`（图片和原始模型 `thoughtSignature` 一并保留）并随下一次请求发送，不使用服务端 `cachedContent`。默认每页使用独立上下文，OpenAI 为 `store:false`，Gemini 只含当前页。内容页与封面封底遵循同一组上下文设置。OpenAI 兼容服务须支持保存与续接，缺少响应 ID 时报错，不静默降级；两种协议页面失败均保留旧结果并重置对话，下一页从新上下文开始。历史只供字形与符号参考，不能改变本页行序或断行；仍占用上下文与用量，不保证费用降低。完成可能乱序，呈现与导出仍遵循编排。

设置中的 `reasoning_effort` 是可选字符串，默认空值使用服务默认。OpenAI 非空时传递 `reasoning: {"effort": value}`，不限制枚举；Gemini 将 `minimal/low/medium/high` 映射为 `thinkingConfig.thinkingLevel`（Gemini 3），将不小于 `-1` 的整数映射为 `thinkingConfig.thinkingBudget`（Gemini 2.5；`-1` 动态，`0` 关闭是否可用取决于模型）。客户端不按模型名猜测参数。识别和连接测试均不发送最大输出 token 设置（OpenAI `max_output_tokens`、Gemini `maxOutputTokens`），旧保存值不生效；额度遵循服务默认和模型限制。截断时提示检查输出限制或降低推理程度，已返回 usage 仍计入。删除整本书接口成功返回 `204 No Content`；未知书籍返回 `404`，正在处理的同一本书返回 `409`。删除只作用于目标书籍，不改变其他书籍、全局设置或 API 密钥。

设置默认 `api_protocol=openai_responses`、`base_url=https://api.openai.com/v1`、`responses_path=/responses`、`models_path=/models`；旧设置缺协议时沿用 OpenAI，缺模型路径时采用 `/models`。Gemini 协议为 `gemini`，官方根地址 `https://generativelanguage.googleapis.com/v1beta`、模型集合路径 `/models`，无具体模型预设。切换协议不联网，默认服务地址可随协议替换，自定义地址保留。OpenAI 两个相对路径留空时直连根地址；Gemini 不使用 `responses_path`，`models_path` 表示资源集合，空值表示根地址自身就是集合。生成地址为集合加 `/`、编码后的短模型名及 `:generateContent`，手动模型 ID 接受裸名称或 `models/` 前缀。

`POST /api/settings/models` 请求体为 `{api_protocol?:"openai_responses"|"gemini",base_url:string,models_path:string,api_key?:string,clear_api_key?:boolean,timeout_seconds:number}`。前端提交当前草稿，无须先保存；OpenAI 以 Bearer GET 读取 `data[].id`。Gemini 以 `x-goog-api-key` GET 模型集合，跟随 `nextPageToken`（下次传 `pageToken`），仅返回 `supportedGenerationMethods` 包含 `generateContent` 的完整 `name`，结果仍为 `{models:string[]}`。空密钥仅在规范化后的根地址、协议都与已保存配置一致且未清除时复用。获取不修改设置或凭据、不发起推理、不随输入自动联网；失败可手填模型 ID，列表不证明图像或 JSON Schema 能力，敏感上游响应不透传。

`PUT /api/settings` 在有旧密钥且规范化根地址或协议变化时，要求新密钥或 `clear_api_key=true`，否则返回 `400`；没有旧密钥时允许保存空配置。同一连接身份下空白密钥可保留旧值，显式清除优先。读取设置不返回密钥。

所有页面识别请求均按显式协议选择客户端。OpenAI 使用 base64 PNG `input_image` 与 `text.format` 的 `json_schema`（`strict: true`）；Gemini 使用原生 `systemInstruction`、`contents`/`inlineData` 及 `generationConfig`，通过 `responseMimeType: application/json` 和 `responseJsonSchema` 请求结构化输出，不发送 OpenAI 参数。旧 `structured_output` 字段仅用于兼容。连接测试在保存后由用户主动触发非流式文本推理，可能产生用量，不代表图像识别质量。

Gemini 输入 token 取 `usageMetadata.promptTokenCount`，输出取 `candidatesTokenCount + thoughtsTokenCount`（thoughts 缺失按 0；candidates 未知则输出未知），总量取 `totalTokenCount`，未知字段保持未知，不估算。协议参考：[Google 模型列表](https://ai.google.dev/api/models)、[Google generateContent](https://ai.google.dev/api/generate-content)。

## 安全与限制

- 默认监听 `127.0.0.1`；这是本地单用户工具，不提供账号或多用户权限。
- API 密钥按用户授权保存到 SQLite 的独立凭据记录中，本机明文存储；启动时加载，不进入前端存储、书籍导出和日志。空白/省略保留，非空替换，`clear_api_key=true` 优先清除；设置和凭据在同一事务中提交，失败时不更新内存。升级到支持持久化密钥的版本后，重启新版并重新输入保存一次旧内存 key 即可，之后不必每次重启重填。
- API 根地址限制为 `http(s)`，拒绝 userinfo、query、fragment；请求不自动重定向，避免泄漏 Authorization。
- 模型输出按不可信输入检查响应状态和文本长度；拒绝内容或 incomplete 响应应成为可见失败。
- LaTeX 源码不再设命令或环境白名单，支持完整文档、宏包和自定义宏；实际语法与依赖由 XeLaTeX 处理。编译仍禁用 shell-escape、隐藏 Windows 进程并设超时，不自动安装任意源码所需依赖或上传资源，不自动改写正文。
- 运行中的任务重启后标记 `interrupted`；有限重试只覆盖暂时错误，不做无限重试。
- PDF 上传仅建立宽高为 0 的轻量页面记录。原页预览独立按需渲染；识别时逐页更新实际宽高并补齐所选页缺少的 PDF、PNG，单页文件保留源页尺寸、旋转和裁切，已有资产复用。上传、选择和预览均不发起模型请求。
- 不依赖 Redis、Celery、微服务、插件系统或通用工作流引擎。

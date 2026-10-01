# 详细功能与配置参考

这是一个本地单用户工具：以项目管理多个 PDF、PNG 或 JPEG 文件，确认上传后先选择、预览和编排页面，再交给页面代理逐页转录为 LaTeX，经过人工校对后生成整书 LaTeX、PDF 和结构化 JSON。文件可以排序，PDF 内页可以排序，不同文件的页面也可以相互穿插。普通页面代理先判断页面类型：内容页直接忠实转录；发现封面或封底后停止普通转录，交给独立的特殊页面子代理读取同一页的书名、作者、出版社等核心书目信息，自动填入书目校对栏。正文为 LaTeX 片段，书目与页眉页脚为结构化纯文本。后端按整书纸型、字体、字号、行距和边距生成统一文档，用本机 XeLaTeX 编译实际 PDF；前端展示用户主动生成的本页草稿或已保存整书 PDF。

第一版只接受 PDF、PNG、JPEG。EPUB/MOBI 原生导入留到后续版本；不要把它们改名后上传来绕过类型检查。

## 运行环境

- Python 3.11 或更新版本，以及 `backend/requirements.txt` 中列出的依赖。
- Node.js 20.19 或更新版本（或 22.12 及更新版本），以及 pnpm。当前锁文件中的 Vite 8.3.0 和 `@vitejs/plugin-react` 6.1.1 要求 Node `^20.19.0` 或 `>=22.12.0`；仓库用 `pnpm-lock.yaml` 固定前端依赖。
- PDF 预览与下载需本机已有 XeLaTeX、`ctexbook`、Fandol 字体和模板所需宏包。可把 `xelatex` 加入 PATH，或以 `EBOOK_OCR_XELATEX` 指定可执行文件路径；不填写参数或 shell 命令。程序不自动安装 TeX 或宏包。缺少编译器时仍可 OCR、校对及导出 LaTeX、JSON。
- 可访问所配置 OpenAI Responses 或 Google Gemini 原生 API 的网络连接、API Key 和一个可用的页面代理模型。项目不会自动切换到 Chat Completions。

后端默认只监听 `127.0.0.1`。前端 Vite 配置固定使用本机的 `127.0.0.1:5173`，并把 `/api` 代理到 `127.0.0.1:8000`。

## 启动

Windows 直接双击根目录唯一入口 `ocr.bat`，会显示前后端状态和操作菜单：回车或输入 `1` 启动 / 打开页面，`2` 停止服务，`3` 重启服务，`4` 刷新状态，`0` 退出管理窗口。关闭管理窗口不会停止服务；停止或重启会中断正在识别的任务。

启动时会自动验证 Python 3.11+、Node.js 20.19+（或 22.12+）、pnpm 和前端依赖，在项目内创建 `.venv` 并按需安装后端依赖，以隐藏窗口启动 `127.0.0.1:8000` 和 `127.0.0.1:5173`，等待健康检查通过后打开浏览器。日志和进程记录位于 `.cache/launcher/`。脚本不受当前终端目录影响。

也可在 PowerShell 中使用无交互命令：`.\ocr.bat Start` 启动 / 打开页面，`.\ocr.bat Start -NoBrowser` 无浏览器启动，`.\ocr.bat Stop` 停止，`.\ocr.bat Restart` 重启，`.\ocr.bat Status` 只查看状态。重启同样支持 `-NoBrowser`；命令执行失败会返回非零退出码，不会停在菜单等待输入。

重复启动会复用正常运行的前后端。即使 `.cache/launcher/pids.json` 丢失，脚本也会核验端口进程的完整启动命令与当前项目路径，恢复本项目服务记录；停止时同样核验归属，并处理 Windows Python 虚拟环境的实际监听子进程。若只有一部分服务运行或健康检查失败，选择“重启服务”。启动失败不会清除原有记录；无法确认归属的进程不会被接管或停止。重启会等待端口释放，若仍被占用则停止操作并提示对应 PID。

在仓库根目录打开两个终端。

后端：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

如果 `python` 指向不存在的解释器，请先安装有效的 Python 3.11+，或用有效解释器的绝对路径替换上面命令中的 `python`。

前端：

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm run dev -- --host 127.0.0.1 --port 5173
```

浏览器打开 `http://127.0.0.1:5173`。前端的生产构建命令是：

```powershell
cd frontend
pnpm run build
```

后端入口确定为 `backend.main:app`，依赖文件为 `backend\requirements.txt`。默认运行数据目录是 `backend/data/`，可用环境变量 `EBOOK_OCR_DATA_DIR` 指定其他目录；该目录包含 SQLite 数据库、源 PDF、单页 PDF 和页面 PNG，已由 `.gitignore` 忽略。

LaTeX 升级新增的 `mistune>=3,<4` 只用于旧数据迁移，启动器会按后端依赖文件变化安装依赖；使用 `ocr.bat Restart` 启动新版。本次修改未执行安装、服务重启、测试、构建或 LaTeX 编译。

## 第一次使用

1. 启动前后端并打开前端页面。
2. 在设置中选择 API 协议，再填写根地址、接入路径、页面代理模型、推理程度和超时。默认协议为 OpenAI Responses（`openai_responses`），根地址 `https://api.openai.com/v1`，Responses 路径 `/responses`，模型列表路径 `/models`。选择 Google Gemini 原生（`gemini`）时，官方根地址为 `https://generativelanguage.googleapis.com/v1beta`，模型列表路径为 `/models`，不使用 Responses 路径；应用不预设具体模型。切换协议不会自动联网，自定义根地址保留。
   点击“获取模型”会使用当前草稿，通过本地后端请求模型列表，无须先保存；输入变化不会自动联网，也不会发起推理或保存草稿及密钥。Gemini 自动读取分页并仅列出支持 `generateContent` 的完整 `models/...` 名称；手动填写可使用裸模型 ID 或 `models/` 前缀。列表不证明图像或结构化输出能力。空密钥仅在规范化后的根地址、协议均与已保存配置一致且未选择清除时复用；更换连接身份时需重新输入密钥或明确清除旧密钥。
3. 首次打开时模型名称和 API 密钥为空是正常状态。用户必须自行选择并填写可用的页面代理模型和密钥；应用不替用户选定某个供应商或模型。旧的分类模型字段可以保留但不参与新流程。保存后的密钥写入默认 `backend/data/app.db` 中独立的 SQLite 凭据记录，按本机明文保存（不提供加密或 keyring）；GET 设置、导出 JSON、浏览器 `localStorage` 和日志都不应包含它。后端重启会从该记录加载，之后不必重复输入。
4. 保存配置后主动点击“测试连接”。测试使用已保存配置发起文本推理，可能产生用量，不代表图像识别质量；测试不会替用户开始处理书籍。
5. 新建项目，在项目内批量上传 PDF、PNG 或 JPEG，检查文件清单后确认上传。新上传页面默认全部勾选，只有勾选页进入识别、校对及导出；取消勾选即可排除，确认编排至少保留一页。追加文件保留已保存的选择和顺序，只将新页面勾选并加入末尾。上传不会开始识别；PDF 只保存源文件、页数和轻量页面记录。PDF 与图片统一排列，类型只作为文件标识。
6. 进入页面编排，在统一文件树中排序、选择文件内页面，也可把另一个文件嵌入顶层文件的页面之间；嵌入的文件在左侧显示为子级，并可移回根级。只允许一层子文件；根文件携带子文件移入另一根文件时，全部成为目标文件的同级子文件。移动文件保留原有选页范围，最终页序立即更新；不同来源的单页仍可混排。拖动时显示落点、让位和落位动画，跟随系统的减少动态效果设置。所有修改先实时展示为本地草稿，点击底部“确认编排，进入校对”才保存。页面缩略图与大图按需生成，不调用模型；后续处理与导出均遵循最终页序，来源文件名和源页码保留。
7. 使用“识别未完成页”批量处理未完成页；全部完成后该按钮隐藏，不提供整批重新识别。每个当前校对页另有“识别本页”，已有结果时显示“重新识别本页”，确认覆盖后只处理当前页。完成后先检查页面类型，可纠正为普通内容页、封面或封底。内容页编辑 LaTeX 正文、公式、表格和脚注，可用工具按钮插入对齐、分段、无缩进和独立公式；页眉页脚目前只能在 PDF 或 JSON 中核对，尚不支持人工编辑。封面和封底由特殊页面子代理自动读取并填入书目校对栏，无须手工复制，只需检查字段，不录入简介、推荐语、定价或联系方式。“更新本页 PDF”可预览未保存草稿，不保存修改。修改后保存页面，仅正在识别的页面不能保存校对，其余页面可在识别期间编辑并保存；待识别页一旦人工保存，本轮将跳过其识别以保留校对结果。暂停立即停止启动新页，等待所有已开始页面（包括特殊页面子代理）结束并保存结果；单页重新识别成功替换旧结果、失败保留旧结果，两阶段请求及重试的用量和尝试次数均累计到同一页。
8. 在校对工具栏或整书预览选择整书纸张尺寸，选中后即保存；默认 A4，另提供 A5、A6、ISO B5、ISO B6 和 6 × 9 英寸。整书排版表单可调整字体、字号、行距倍率、首行缩进、段距和页边距，点击“保存排版”后生效；字号与边距留空时随纸型使用默认值。排版设置独立于页面校对，不调用模型；本页和整书 PDF 共用已保存排版。
9. 在整书预览点击“生成/更新 PDF”，检查已保存且已编排页面，下载 LaTeX、当前 PDF 或 JSON。内容、纸型、页序、排版或装订选项变化后，旧 PDF 预览失效，需重新生成；编译失败显示错误，不展示旧 PDF 作为最新结果。默认居中，开启“打印装订版”后按已识别源页侧别采用镜像装订边；详情见 [左右页与打印版](PRINT_LAYOUT.md)。可返回编排调整选择和顺序，未勾选页保留历史结果、缓存和用量。追加文件后须重新确认上传与编排，原有页序和结果保留。每个项目可确认后单独删除，运行中禁止删除。

## 配置与数据

当前设置字段、页面和用量字段见 [接口与模块说明](module-architecture.md)；根目录 [ARCHITECTURE.md](../ARCHITECTURE.md) 保留历史设计。API 根地址必须是 `http` 或 `https`，不能带用户信息、查询串或片段；路径字段是相对路径。OpenAI 的 Responses 路径留空时直连根地址；Gemini 的 `models_path` 表示模型资源集合，留空时根地址自身就是集合，生成请求会在集合后追加 `/{model}:generateContent`。旧设置缺少 `api_protocol` 时采用 `openai_responses`，缺少 `models_path` 时采用 `/models`。

`reasoning_effort` 留空使用服务默认。OpenAI 非空值按 `reasoning: {"effort": value}` 传递；Gemini 的 `minimal/low/medium/high` 映射到 Gemini 3 的 `thinkingLevel`，整数且不小于 `-1` 映射到 Gemini 2.5 的 `thinkingBudget`（`-1` 动态、`0` 关闭，具体支持取决于模型）。应用不根据模型名猜测参数。识别与连接测试均不设置最大输出 token（省略 OpenAI `max_output_tokens` 和 Gemini `maxOutputTokens`）；旧保存值不生效，额度遵循服务默认及模型限制。应用数据保存在 `backend/data/`，可通过 `EBOOK_OCR_DATA_DIR` 更改。

设置中的 `processing_concurrency` 默认 10，只接受正整数且无固定上限，各项目实际并发不超过该值。`context_reuse_enabled` 默认 `false`；开启后按本次待处理页的最终编排顺序连续分组，组内串行、组间并行，`context_reuse_max_pages` 为 1–10 的整数，默认 10，包含首张。保存设置不联网，修改从下次任务生效。默认每页独立上下文。OpenAI 普通代理默认发送 `store:false`；实验性复用使用 `store:true` 和 `previous_response_id`，需要服务支持保存与续接，缺少响应 ID 时报错，不静默降级。Gemini 复用在每组客户端本地保留完整 `contents`（含图片和原始模型 `thoughtSignature`）并随下一页重新发送，不是服务端 `cachedContent`。历史只供排版和符号参考，仍占用上下文与用量，不保证节省费用；失败后下一页重置对话，新任务不继承历史。特殊页面子代理始终使用新的同协议独立客户端、不带任何历史：OpenAI Responses 为 `store:false` 且无 `previous_response_id`；Gemini 的 `contents` 只含当前页，不携带普通代理历史。其结果不接入普通代理历史。两类代理共用已配置的协议、模型、地址及推理程度，不新增模型选项，也不会创建 Codex 任务。

普通页面代理按严格 JSON Schema 返回 `page_kind`、`page_side`、`header_segments`、`body_latex`、`footer_segments` 五个字段；普通内容页一次完成转录，封面封底返回类型、`page_side: unknown` 及空正文、空页眉页脚。后端收到该分流结果后立即调用 `SpecialPageAgent`，提供同一页图像、来源信息和分流类型，由它按独立 Schema 返回 `page_kind`、`cover_fields`。当前请求为非流式，分流发生在第一阶段响应返回后，不表示实时中断生成。普通页通常一次请求，特殊页通常两次，两阶段及各自重试均累计到同页用量和尝试次数；中间分流结果不落库，也不计作完成。专用提取失败时保留旧结果。

页眉页脚各项包含 `kind`、`text`、`alignment`、`row`、`font_size`、`bold`、`italic`，模型输出时字段均为必填。`alignment` 为相对于整页可排印宽度的 `left`、`center` 或 `right` 锚点；`row` 为对应区域内 1–10 的绝对行号，行号间隔保留为空行；`font_size` 为 `small` 或 `normal`；粗体和斜体使用布尔值。旧结果缺少新增格式字段时按居中、第一行、小号字、非粗体、非斜体显示。模型 `body_latex` 存为 `Page.text`，只包含受支持的 LaTeX 正文片段，书目及页眉页脚保持纯文本；手写批注及类似后加笔迹不进入结果。应用分别保存页面类型、页侧、书目、正文及页眉页脚，`Page.page_side` 与数据库列 `pages.page_side` 取值为 `left|right|unknown`；旧结果默认 `unknown`，不依据旧页脚或页序推断，也不自动重新识别。LaTeX 与 PDF 使用相同后端模板，JSON 保留各结构字段，当前没有 Markdown 或 HTML 下载。旧设置中的 `structured_output` 字段保留兼容，页面请求固定使用结构化输出。模型响应是不可信输入，系统会检查响应状态、结构和文本长度；重复处理成功时替换旧结果，失败时保留旧结果。`usage` 只记录供应商实际返回的 token 字段，缺失字段保持 `null`；重试和重复处理会累计已知用量与尝试次数，无法确认的请求会让 `complete` 为 `false`。重启时运行中的任务会标记为 `interrupted`；已暂停状态会保留。

API 密钥只写入本机 SQLite 的独立凭据表，使用明文存储；它不会写入单独的密钥文件、浏览器存储、日志或书籍导出。空白或未提交密钥会保留已有值，明确清除才会删除记录。升级到支持持久化密钥的版本时，需先重启新版后端，并把旧进程中仍在使用的密钥重新输入并保存一次；保存后后续重启无需再次填写。请按本机数据库访问权限保护 `backend/data/app.db`。

页面结果新增 `page_kind`（`content`、`front_cover`、`back_cover`）和 `cover_fields`（`{kind,text}` 数组）。模型按当前图像的排版角色判断，不依赖首尾页码、文件名或相邻页。`front_cover` 包括正面外封面，以及以全书书名、署名、出版社等为视觉主体且无连续正文的独立书名页、内封、扉页；黑白、纯文字或没有封皮边缘不影响判断。章节标题页、版权页、目录及不确定页面仍按内容页处理。封面封底只保存本页可见的核心书目字段，保留印刷书法体、艺术字及册卷信息，排除后加笔迹、馆藏章和馆藏编号；正文和页眉页脚为空，确认是封底但没有核心信息时，书目数组也可以为空。旧页面类型迁移为 `content`，不自动重新识别；需手动校正类型及字段，或主动重新识别，才能得到新分类。单页 PDF、整书 PDF 和 LaTeX 使用同一套页面类型排版，封面封底的纯文本书目由模板转义，不因正文为空而漏掉。

纸张尺寸按项目持久化为 `Book.paper_size`，旧项目增量迁移为 A4，不改变原有识别结果和状态。常用纸型的毫米尺寸及操作说明见[整书纸张尺寸](usage.md#整书纸张尺寸)。纸型影响重排后的纸张，原页预览保持源文件比例；正文较长时仍可跨打印页。

整书排版保存在 `Book.layout`，包括宋体/黑体/楷体、字号（默认 `null`，随纸型）、行距倍率（默认 1.6）、首行缩进（默认 2 汉字）、段距（默认 0 pt）与边距（默认 `null`，随纸型）。前端 pt 对应 LaTeX bp，基础基线距离为字号乘以行距倍率。`PUT /api/books/{id}/layout` 至少提供 `paper_size` 或 `layout`，前端提交完整排版对象。PDF 编译和下载接口见 [HTTP 契约](module-architecture.md#http-契约索引)，正文语法、范围及默认值见 [LaTeX 排版说明](LATEX_LAYOUT.md)。

`Book.content_format` 统一为 `latex`。旧 Markdown 正文仅在启动时一次性迁移：写库前生成数据目录的 `app-before-latex.db` 备份，原文留在内部 `pages.legacy_markdown` 供恢复，不对 API 公开。迁移在事务内完成，不调用模型，保留人工校对、状态、用量和页序；旧公式、表格与脚注的转换仍需人工核对。数据库及备份可能含明文凭据，不可当公开发布包。

## 已知范围

- 仅支持 PDF、PNG、JPEG 导入；EPUB/MOBI 是后续工作。
- PDF 固定 500 页上限已取消；上传文件仍限制为 100 MB，图片与 PDF 渲染仍保留像素限制。旧书已有的单页 PDF 和 PNG 会复用。
- 导出包括 LaTeX 源文件、实际编译 PDF 和结构化 JSON。前端不继续提供 Markdown 编辑、预览、导出或 HTML 下载；旧格式只作迁移输入和历史记录。PDF 使用本机 XeLaTeX，缺少编译器不阻止 LaTeX、JSON 导出。
- 默认每个项目最多并发处理 10 页，可配置任意正整数；多个项目分别限制，完成可能乱序，呈现与导出仍按编排顺序。默认每页独立上下文，可开启实验性的分组上下文复用；不自动合并跨页段落。原书页眉、页脚和其中的页码单独保存为结构化语段，脚注留在正文中。
- 可从书库单独删除一本书；删除会移除该书记录、页面记录、请求用量记录以及源文件和页面文件。正在处理的书籍返回冲突并由界面禁用删除操作；删除不会影响其他书籍、全局设置或 API 密钥。
- 按显式选择的协议发送非流式请求。OpenAI 使用 base64 PNG `input_image` 和 `text.format` JSON Schema（`strict: true`）；Gemini 使用原生 `systemInstruction`、`contents`/`inlineData` 和 `generationConfig`（`responseMimeType: application/json`、`responseJsonSchema`），不混入 OpenAI 参数。Gemini 仅支持 API Key 开发者 API，不包含 Vertex OAuth、多账户或 Chat Completions 回退。若输出被截断，应检查模型限制或降低推理程度；已返回用量仍会计入。Gemini 输入用量取 `promptTokenCount`，输出取 `candidatesTokenCount + thoughtsTokenCount`（缺失 thoughts 按 0，缺失 candidates 则输出未知），总量取 `totalTokenCount`。
- 公式在 LaTeX 数学模式中保存，中文使用 `ctexbook` 与 Fandol。正文限制为受支持命令及环境，不包含文档导言区、宏定义、任意文件命令或外部资源命令；编译禁用 shell-escape，设有超时并显示失败信息。
- 预览与导出保留页眉、正文、页脚结构，源页之间分隔，但不精确复刻原页版面，也不承诺每个源页都对应一张打印纸；长正文由 LaTeX 自然续页，续页装订边沿用源页侧别。复杂跨页表格和跨页段落需要人工校对。
- 简单表格用 `tabular`，长表可用 `longtable`；复杂合并表格、图表和插图保留可见文字、标签和图注，不根据图形猜测数据或重绘图表。脚注保留原印刷标号和注释文字，不自动生成新编号。
- 本次 LaTeX 迁移未运行测试、构建、编译、真实识别或 GUI 验证；历史验收及发布记录只说明对应旧实现。
- 这是本地单用户工具，不包含账号、多用户权限、后台队列、Redis、Celery、WebSocket、插件系统或通用工作流引擎。

更多操作步骤、故障处理和人工校对要点见 [操作说明](usage.md)；当前多文件契约与验收要求见 [架构模块说明](module-architecture.md) 和 [验收清单](acceptance.md)。[examples/sample-export.json](../examples/sample-export.json) 保留为旧单文件页面结果示例。


# 电子书识别与重排：第一版设计与模块契约

> 当前项目、多文件上传与页面编排契约见 [架构模块说明](docs/module-architecture.md)，逐页识别以 [逐页代理设计](docs/PAGE_AGENT_DESIGN.md) 为准。下文为旧版设计记录：单文件直接处理、两阶段模型调用、bbox/裁图与块编辑流程已由新版替代，不作为当前接口或任务分工要求。

当前入口为“新建项目 → 多文件上传 → 确认上传 → 文件排序、PDF 内页选择与排序、跨文件页面混排 → 确认编排 → 逐页处理与校对 → 整书预览和导出”。项目复用 `Book`，来源文件由 `SourceFile` 表示；`Page.number` 是项目内永久页面 ID，原始来源用 `source_filename` 和 `source_page` 表示，最终阅读顺序以已选页面数组为准。PDF 页面内容预览使用现有 PyMuPDF 按需生成独立小图缓存，不提前准备全部 OCR 文件或请求模型。旧单文件数据和已完成结果继续保留。

## 范围与原则
本地单用户工具，前后端分离。输入 PDF、PNG、JPEG；EPUB/MOBI 原生导入放到后续版本，不伪装支持。导出结构化 JSON、独立 HTML，通过浏览器打印生成 PDF。前端 React + TypeScript + Vite + KaTeX，后端 Python + FastAPI + Pydantic + httpx + PyMuPDF + Pillow，SQLite 保存书籍与分页结果，文件系统保存来源与页面图片。默认只绑定 127.0.0.1。不要微服务、Redis、Celery、插件系统或通用工作流引擎。

## 内容处理与排版
上传（PDF 仅读取页数、保存源文件与轻量页记录）→ 选择范围并点击处理 → 按所选页逐页准备单页 PDF 和 PNG → 视觉模型忠实提取 → 文本模型按块分类 → 校对 → 排版与导出。两个阶段必须独立保存，分类只返回块 ID、类型和标题层级，不得改写提取文字。当前逐页处理按项目有界并发，默认上限 10；默认独立上下文，实验性分组复用见当前模块说明；重启将运行中任务标记 interrupted。不自动合并跨页段落，不自动删除页眉脚注；可在排版隐藏 header/footer/page_number。

内容类型：heading、paragraph、quote、example、list、code、equation、table、figure、caption、footnote、header、footer、page_number、unknown。每块保留来源页、顺序、原文与归一化 bbox [x0,y0,x1,y1]（0..1）；低置信内容标记待校对。公式使用 LaTeX（行内 \\(…\\)，独立 equation），不执行模型 HTML。KaTeX trust=false，解析失败显示原文与来源区域。表格 rows 为字符串二维数组；复杂合并表格、图表、插图优先来源裁图，caption 独立保留。不凭图形猜测数据或重绘图表。

HTML/CSS 是排版框架：中文衬线正文、清晰标题层级、引用左边线、实例轻灰底、脚注小号、表格重复表头、图注和图片同组，印刷 @page A4、合理页边距、孤行控制、标题避免页末、图/公式尽量不跨页。超过单页的表格允许分割，宽表横向滚动供屏幕阅读，打印限制宽度。重排后自然分页，原页码仅用于来源追溯。导出 HTML 内嵌 CSS、图片 data URL，公式由 KaTeX 转为 MathML，不能依赖开发服务器或外部脚本；MathML 显示依赖现代浏览器及系统数学字体。PDF 是浏览器打印功能而非承诺后端 PDF 引擎。

## 数据与 HTTP 契约（统一 /api 前缀）
Settings: {base_url:string,responses_path:string,models_path:string,extraction_model:string,classification_model:string,api_key?:string,has_api_key?:boolean,structured_output:boolean,timeout_seconds:number,processing_concurrency:number,context_reuse_enabled:boolean,context_reuse_max_pages:number}。`processing_concurrency` 默认 10、正整数、无固定上限；`context_reuse_enabled` 默认 false；`context_reuse_max_pages` 为 1–10 的整数、默认 10、含首张。旧设置缺字段使用默认值，保存不联网，修改下次任务生效。默认 base_url=https://api.openai.com/v1，responses_path=/responses，models_path=/models，模型由用户选择或手动填写；models_path 随设置保存，旧设置缺字段时采用默认值。GET 不返回密钥；PUT 空密钥表示保留，非空值替换，clear_api_key=true 优先清除。密钥按用户授权持久化到默认 `backend/data/app.db` 的独立 SQLite 凭据记录，使用本机明文存储；启动时加载，设置与密钥在同一事务中保存，失败时不更新内存。不得存入前端 localStorage、导出文件或日志。升级到支持持久化密钥的版本后，需重启新版并重新输入保存一次旧进程中的密钥，之后无需每次重启重填。两个接入路径各自可自定义相对路径，留空时对应请求直接使用根地址；base_url 限 http(s)，拒绝 userinfo/query/fragment，不自动重定向以避免泄漏 Authorization。连接测试由用户主动触发，使用已保存配置进行文本推理，可能产生用量，不代表图像识别质量。

Book: {id,title,filename,status,page_count,completed_pages,error,created_at}。
Page: {number,width,height,image_url,status,error,extraction_text,blocks:Block[]}。
Block: {id,type,text,bbox:number[]|null,confidence:number|null,needs_review:boolean,level:number|null,latex:string|null,rows:string[][]|null,asset_url:string|null}。数组顺序为阅读顺序。所有 nullable 字段可为 null；响应始终包含约定字段。book.status/page.status 使用 uploaded、extracting、classifying、ready、failed、interrupted；book 可 processing。空白页允许空 blocks。

- GET /api/health → {status:"ok"}
- GET /api/settings → Settings；PUT /api/settings → Settings
- POST /api/settings/test → {ok:boolean,message:string}，使用已保存设置
- POST /api/settings/models，{base_url,models_path,api_key?:string,clear_api_key?:boolean,timeout_seconds:number} → {models:string[]}，用当前草稿 GET 上游模型列表并读取 data[].id，无须先保存，不保存草稿或凭据，不发起 Responses 推理。空密钥只在根地址与已保存地址一致且非 clear 时复用已保存密钥，换地址后必须重新输入。只由用户点击触发，不自动随输入联网；列表不可用时可手动输入模型 ID，列表不证明图像、Responses 或 JSON Schema 支持情况。
- GET /api/books → Book[]
- POST /api/books，multipart file → Book
- GET /api/books/{id} → {book:Book,pages:Page[]}
- POST /api/books/{id}/process → {started:boolean}；已运行时 409。仅处理未完成页，已提取页可续分类。
- PUT /api/books/{id}/pages/{number}，JSON {blocks:Block[]} → Page；运行中拒绝修改
- GET /api/books/{id}/export → {book:Book,pages:Page[]}，结构化 JSON 下载
- GET /api/books/{id}/assets/{name} → 安全受限的来源图/裁图文件

错误统一使用 FastAPI detail 文本；所有 ID、路径必须验证并限制在应用数据目录。文件大小限制为 100 MB，图片像素、PDF 渲染像素、模型结果长度设合理上限；取消 PDF 固定 500 页上限。前端以轮询展示页状态；不引入 WebSocket。

## Responses 协议
推理仅 POST base_url + responses_path；responses_path 为空或纯空白时归一化为空，直接 POST 规范化后的 base_url。请求包含 Authorization Bearer、model、input；页面代理使用 input_text + input_image（base64 PNG data URL），连接测试使用 input_text。默认独立上下文 store=false；实验性复用按本次待处理页编排顺序连续分组，组内串行、组间并行，每组独立客户端使用 store=true 和 previous_response_id，需要服务支持保存与续接，缺少响应 ID 报错而不降级。历史占用上下文与用量，只供排版与符号参考，只输出当前页；失败后下一页重置对话。每项目同时处理页数不超过配置，多个项目各自限制；暂停不再启动新页，等待已开始页结束，新任务不复用旧响应 ID；完成可乱序，呈现与导出按编排。页面请求统一使用 text.format 的 json_schema 和 strict:true，不按供应商域名调整协议，不提供供应商预设或 Chat Completions 回退；旧 structured_output 字段仅保留兼容。遍历 output 中 message/content/output_text，识别拒绝、incomplete、HTTP 错误；不能把 reasoning 当结果。仅对暂时错误有限重试，不打印密钥或向用户透传原始敏感响应。模型输出是不可信数据，校验 JSON、结构和内容长度；失败不得破坏既有页面结果。

官方参考：https://developers.openai.com/api/docs/guides/images-vision ，https://developers.openai.com/api/docs/guides/structured-outputs ，https://developers.openai.com/api/reference/cli/resources/responses/methods/create 。

## 任务分工与验收边界
1. sol xhigh / backend：backend/ 全部后端、依赖、少量关键测试。实现导入、持久化、Responses 客户端、两阶段流水线、校对保存和 JSON 导出。对复杂流程负责。提供确切启动命令和接口偏差。
2. sol xhigh / frontend：frontend/ 全部前端，包括任何样式、排版、浏览器 HTML 导出代码。中文简洁界面，无渐变和花哨动效。书库/导入、流程进度、原页与块校对、重排预览、设置。处理空态、加载、失败和重试；密钥输入 password；设置清楚区分 API 根地址和 Responses 接入路径。自行执行一次类型检查/构建和必要冒烟。
3. luna max / support：README.md、.gitignore、docs/ 操作说明、examples/ 纯数据示例；不要修改前后端实现或设计契约。说明限制、运行方式、配置和人工校对流程；提供含正文/引用/实例/公式/表格的示例 JSON。

所有子代理必须：禁止过度测试、禁止过度设计，追求简洁和可维护性。只做与核心风险对应的少量验证，不追求覆盖率、不进行性能压测或重复完整测试，不真实调用付费模型。不得擅自扩展范围。按目录分工，接口变化先协调。主代理负责总体设计、任务要求、协调与审阅，不参与基础编码。

# 电子书识别与重排：第一版设计与模块契约

> 本轮重构以 [逐页代理设计](docs/PAGE_AGENT_DESIGN.md) 为准。下文为旧版设计记录：两阶段模型调用、bbox/裁图与块编辑流程由新版替代。

## 范围与原则
本地单用户工具，前后端分离。输入 PDF、PNG、JPEG；EPUB/MOBI 原生导入放到后续版本，不伪装支持。导出结构化 JSON、独立 HTML，通过浏览器打印生成 PDF。前端 React + TypeScript + Vite + KaTeX，后端 Python + FastAPI + Pydantic + httpx + PyMuPDF + Pillow，SQLite 保存书籍与分页结果，文件系统保存来源与页面图片。默认只绑定 127.0.0.1。不要微服务、Redis、Celery、插件系统或通用工作流引擎。

## 内容处理与排版
上传 → 页面栅格化 → 视觉模型忠实提取 → 文本模型按块分类 → 校对 → 排版与导出。两个阶段必须独立保存，分类只返回块 ID、类型和标题层级，不得改写提取文字。逐页处理，默认串行，失败可重试且复用成功阶段；重启将运行中任务标记 interrupted。不自动合并跨页段落，不自动删除页眉脚注；可在排版隐藏 header/footer/page_number。

内容类型：heading、paragraph、quote、example、list、code、equation、table、figure、caption、footnote、header、footer、page_number、unknown。每块保留来源页、顺序、原文与归一化 bbox [x0,y0,x1,y1]（0..1）；低置信内容标记待校对。公式使用 LaTeX（行内 \\(…\\)，独立 equation），不执行模型 HTML。KaTeX trust=false，解析失败显示原文与来源区域。表格 rows 为字符串二维数组；复杂合并表格、图表、插图优先来源裁图，caption 独立保留。不凭图形猜测数据或重绘图表。

HTML/CSS 是排版框架：中文衬线正文、清晰标题层级、引用左边线、实例轻灰底、脚注小号、表格重复表头、图注和图片同组，印刷 @page A4、合理页边距、孤行控制、标题避免页末、图/公式尽量不跨页。超过单页的表格允许分割，宽表横向滚动供屏幕阅读，打印限制宽度。重排后自然分页，原页码仅用于来源追溯。导出 HTML 内嵌 CSS、图片 data URL，公式由 KaTeX 转为 MathML，不能依赖开发服务器或外部脚本；MathML 显示依赖现代浏览器及系统数学字体。PDF 是浏览器打印功能而非承诺后端 PDF 引擎。

## 数据与 HTTP 契约（统一 /api 前缀）
Settings: {base_url:string,responses_path:string,extraction_model:string,classification_model:string,api_key?:string,has_api_key?:boolean,structured_output:boolean,timeout_seconds:number}。默认 base_url=https://api.openai.com/v1，responses_path=/responses，模型由用户填写。GET 不返回密钥；PUT 空密钥表示保留，非空值替换，clear_api_key=true 优先清除。密钥按用户授权持久化到默认 `backend/data/app.db` 的独立 SQLite 凭据记录，使用本机明文存储；启动时加载，设置与密钥在同一事务中保存，失败时不更新内存。不得存入前端 localStorage、导出文件或日志。升级到支持持久化密钥的版本后，需重启新版并重新输入保存一次旧进程中的密钥，之后无需每次重启重填。接入点可自定义相对路径；base_url 限 http(s)，拒绝 userinfo/query/fragment，不自动重定向以避免泄漏 Authorization。连接测试由用户主动触发。

Book: {id,title,filename,status,page_count,completed_pages,error,created_at}。
Page: {number,width,height,image_url,status,error,extraction_text,blocks:Block[]}。
Block: {id,type,text,bbox:number[]|null,confidence:number|null,needs_review:boolean,level:number|null,latex:string|null,rows:string[][]|null,asset_url:string|null}。数组顺序为阅读顺序。所有 nullable 字段可为 null；响应始终包含约定字段。book.status/page.status 使用 uploaded、extracting、classifying、ready、failed、interrupted；book 可 processing。空白页允许空 blocks。

- GET /api/health → {status:"ok"}
- GET /api/settings → Settings；PUT /api/settings → Settings
- POST /api/settings/test → {ok:boolean,message:string}，使用已保存设置
- GET /api/books → Book[]
- POST /api/books，multipart file → Book
- GET /api/books/{id} → {book:Book,pages:Page[]}
- POST /api/books/{id}/process → {started:boolean}；已运行时 409。仅处理未完成页，已提取页可续分类。
- PUT /api/books/{id}/pages/{number}，JSON {blocks:Block[]} → Page；运行中拒绝修改
- GET /api/books/{id}/export → {book:Book,pages:Page[]}，结构化 JSON 下载
- GET /api/books/{id}/assets/{name} → 安全受限的来源图/裁图文件

错误统一使用 FastAPI detail 文本；所有 ID、路径必须验证并限制在应用数据目录。文件大小、页数、图片像素、模型结果长度设合理上限。前端以轮询展示页状态；不引入 WebSocket。

## Responses 协议
仅 POST base_url + responses_path；responses_path 为空或纯空白时归一化为空，直接 POST 规范化后的 base_url。请求包含 Authorization Bearer、model、input；第一阶段使用 input_text + input_image（data URL），第二阶段使用 input_text。store=false。结构化模式使用 text.format 的 json_schema，兼容模式使用提示词要求 JSON 并严格校验，不自动切到 Chat Completions。遍历 output 中 message/content/output_text，识别拒绝、incomplete、HTTP 错误；不能把 reasoning 当结果。仅对暂时错误有限重试，不打印密钥或原始敏感响应。模型输出是不可信数据，校验 JSON、bbox、块 ID 和内容长度；分类失败不得破坏提取结果。

官方参考：https://developers.openai.com/api/docs/guides/images-vision ，https://developers.openai.com/api/docs/guides/structured-outputs ，https://developers.openai.com/api/reference/cli/resources/responses/methods/create 。

## 任务分工与验收边界
1. sol xhigh / backend：backend/ 全部后端、依赖、少量关键测试。实现导入、持久化、Responses 客户端、两阶段流水线、校对保存和 JSON 导出。对复杂流程负责。提供确切启动命令和接口偏差。
2. sol xhigh / frontend：frontend/ 全部前端，包括任何样式、排版、浏览器 HTML 导出代码。中文简洁界面，无渐变和花哨动效。书库/导入、流程进度、原页与块校对、重排预览、设置。处理空态、加载、失败和重试；密钥输入 password；设置清楚区分 API 根地址和 Responses 接入路径。自行执行一次类型检查/构建和必要冒烟。
3. luna max / support：README.md、.gitignore、docs/ 操作说明、examples/ 纯数据示例；不要修改前后端实现或设计契约。说明限制、运行方式、配置和人工校对流程；提供含正文/引用/实例/公式/表格的示例 JSON。

所有子代理必须：禁止过度测试、禁止过度设计，追求简洁和可维护性。只做与核心风险对应的少量验证，不追求覆盖率、不进行性能压测或重复完整测试，不真实调用付费模型。不得擅自扩展范围。按目录分工，接口变化先协调。主代理负责总体设计、任务要求、协调与审阅，不参与基础编码。

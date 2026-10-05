# 纸页重排 · 电子书 OCR

将 PDF、PNG、JPEG 交给视觉模型，启动一次任务后自动识别、恢复布局、编译、独立复核并进行有限修复，自动形成 PDF、LaTeX 资源包和 JSON。无法可靠转录的区域或页面保留源图并说明问题，任务不等待逐页校对。适合个人整理扫描书籍、讲义及含公式的资料。

这是 **Windows 优先的本地单用户应用**，由 Python / FastAPI 后端和 React / TypeScript 前端组成。识别使用你自行配置的 OpenAI Responses 或 Google Gemini 原生 API；首次安装依赖和调用模型需要网络。当前源码版本为 `0.1.0`。

## 主要功能

- 一个项目导入多个 PDF 或图片，上传页默认全部选中，可直接开始任务；按文件或单页排序、筛选和穿插编排是可选操作。
- 普通内容页保存 LaTeX 正文、公式和表格；页眉页脚与封面封底书目保持结构化纯文本。
- 默认每个项目并发处理 10 页，任务冻结页序、模型与输出设置；暂停等待已发送请求结算，恢复沿用同一任务和已完成阶段。
- 每次识别、复核、修复和重试均使用独立上下文，只发送当前页及本次任务所需资料；实验性跨页上下文复用已移除。
- 默认物理请求额度为所选页数的三倍；正常页一次识别加一次独立审查，最多一轮局部修复和复审，所有失败请求与暂时错误重试均计入额度。
- 已有人工稿默认受到保护，开始前可明确指定替换范围；运行中仍可主动保存高级编辑，旧后台候选使用修订比较阻止覆盖新稿。
- 支持 A4、A5、A6、B5、B6 和 6 × 9 英寸纸型，可保存字体、字号、行距、首行缩进、段距和页边距；有足够页侧信息时可选择镜像装订边。
- 运行结束自动生成统一修订清单下的 PDF、LaTeX/资源 ZIP 与 JSON，源区域资源随包导出；缺失页、技术失败与带问题完成分别说明。PDF 由本机 XeLaTeX 编译。
- 自动估计几何和字体，保存原行、区域、基线和公式组；仍提供原图对照、布局校准、自由 LaTeX 及完整文档作为可选高级工具。旧项目保留现有模板和历史稿件。

## 快速开始（Windows）

准备以下环境，并确保命令可用：

| 环境 | 要求 |
| --- | --- |
| Python | 3.11 或以上，包含 venv / pip |
| Node.js | 20.19+（20.x）或 22.12+；开发测试建议 22.18+ 或 24.x |
| pnpm | 前端依赖以 `frontend/pnpm-lock.yaml` 为准 |
| XeLaTeX（PDF 功能） | 一键启动会复用现有编译器、补齐模板依赖；未找到编译器时自动下载安装官方 TinyTeX。手动启动后端需自行备齐编译环境 |
| 模型服务 | 可接收图片并支持结构化输出的 Responses 或 Gemini 模型，以及相应 API Key |

1. 将源码解压到电脑的本地文件夹。
2. 双击根目录 **`ocr.bat`**，按回车启动。启动器先准备 Python、前端及 LaTeX 依赖，全部成功后才启动服务；首次准备可能需要联网下载。
3. 健康检查通过后，浏览器打开 `http://127.0.0.1:5173`。
4. 在“设置”中选择协议，填写 API 地址、模型和密钥并保存；“检查已保存配置”只做本地检查，不请求模型。
5. 新建项目 → 上传文件 → 可选调整页序、范围及纸型 → 在任务面板确认本轮范围与调用额度并开始 → 查看自动结果和问题说明 → 下载同一清单的产物。

上传不调用模型，也无需分别确认上传或编排才能开始。开始后不要求逐页批准、填写坐标或解决问题列表；普通内容不确定时其他页面继续，系统有限修复后自动保留源区域或源页。自动通过、带问题完成和技术失败分开显示，能下载 PDF 不等于内容已准确识别。

真正启动服务前，启动器检查依赖指纹和可用模块：`backend/requirements.txt` 变化或后端模块缺失（含旧数据迁移所需 `mistune`）时，仅在项目 `.venv` 中安装 Python 依赖；`frontend/package.json`、`pnpm-lock.yaml` 变化或 Vite 缺失时，执行非交互的 `pnpm install --frozen-lockfile`。成功后在 `.cache/launcher/` 记录依赖指纹，依赖齐全且指纹未变时直接复用，无需联网。Python、Node.js 和 pnpm 仍需预先准备，启动器不会安装这些运行时。已有服务正常运行时直接打开页面，不改动其依赖；需要更新时使用 `ocr.bat Restart`。

LaTeX 准备先使用有效的 `EBOOK_OCR_XELATEX`，未配置或配置路径失效时依次查 PATH 和 Windows 常规 TinyTeX 目录；缺少编译器才下载并校验官方 TinyTeX 包，缺少模板宏包或 Fandol 字体时使用 TeX Live 的 `tlmgr` 补齐。所选目录和变量只在启动进程中设置并由后端继承，不修改系统 PATH 或用户级配置。显式编译器配置无效时警告并继续自动查找；下载失败或无法补齐依赖时停止启动并显示错误。完整查找顺序、自定义发行版与缓存行为见 [LaTeX 排版说明](docs/LATEX_LAYOUT.md#编译环境)。

直接运行后端只负责查找编译器，不下载 TeX 或宏包；缺少 XeLaTeX 时重排候选会报告依赖错误，若源内容仍可读取，系统可生成保留源页的 PDF 并标为带问题完成；源也无法形成输出时才报告技术失败。已有稿件、源文件和可形成的源码/JSON 结果仍保留。启动器的历史安装与检查记录见 [LaTeX 排版说明](docs/LATEX_LAYOUT.md#旧数据迁移与实现状态)。

启动菜单支持启动、停止、重启和刷新状态。关闭菜单窗口不会停止服务；停止或重启会中断正在识别的任务。命令行用法：

```powershell
.\ocr.bat Start
.\ocr.bat Start -NoBrowser
.\ocr.bat Status
.\ocr.bat Stop
.\ocr.bat Restart
```

后端监听 `127.0.0.1:8000`，前端监听 `127.0.0.1:5173`，日志在 `.cache/launcher/`。端口被其他程序占用时，先查看日志和端口归属。

## 手动启动与开发

在项目根目录启动后端：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

另开一个终端启动前端：

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm run dev
```

本轮无人值守重构只进行了编码与静态审查：**未测试、未运行验证**。没有执行测试、构建、试编译、服务启动、模型调用或迁移试跑。仓库中旧测试及 [历史修复报告](docs/RENDERING_REPAIR_VERIFICATION.md) 保留作参考，不代表新版流程已验证。上述启动命令是用户使用说明，不是本轮执行记录。当前启动入口使用本地 Vite 服务；仓库没有提供互联网服务器部署或打包为独立 EXE 的方案。

## 数据保存与迁移

默认数据目录为 `backend/data/`，包含 SQLite 数据库、源文件、页面缓存和识别结果。可以用 `EBOOK_OCR_DATA_DIR` 指定其他目录。API 密钥以明文保存在数据库的独立凭据表中；**完整项目备份可能包含密钥和个人书籍，不要把完整备份当作公开发布包。**

从旧版升级后，下一次后端启动会将已有 Markdown 正文一次性转为 LaTeX。迁移写入前保存同目录的 `app-before-latex.db`，原文留在数据库内部 `pages.legacy_markdown` 供恢复；不对 API 或导出公开。迁移在事务中完成，不重新识别，保留旧稿、用量、状态和页序。备份同样可能含凭据，不能放入公开源码包。旧转换结果没有因此取得自动质量通过状态。

布局升级写入前另保存 `app-before-layout.db`，已有迁移备份不覆盖。新项目默认 `source_fidelity`，旧项目保持 `legacy_template`；自由源码实际修改后转为 `custom_latex`，原布局和稿件归档保留。自动结果按内容修订检查，校准按内容/布局双修订保存，旧页可直接校准。当前没有一键历史恢复界面，数据库回退应停止服务、保留现库并在独立目录核对备份，见[迁移说明](docs/LATEX_LAYOUT.md#旧数据迁移与实现状态)。

无人值守升级在首次写入前保存 `app-before-workflow.db`，增量增加永久页面身份、不可变修订、运行/阶段/请求记录、质量问题和导出清单。既有源文件及历史修订保留；旧 `ready` 只表示旧执行结果，不会迁为质量已通过。旧人工稿默认保护，新任务明确授权的范围才可自动替换。迁移代码未在本轮运行。

新运行 API 为 `POST /api/books/{id}/runs`，接受范围、`expected_arrangement_revision`、可选替换策略与请求额度、`client_request_id`；重复请求返回同一运行。运行列表及详情、`pause`/`resume`、分页 `results`/`issues` 和 `export-manifests` 均可读取。恢复不变更原快照，不静默重发已发送但结果未知的请求；恢复时当前 API 地址和协议必须与原接入一致，新接入密钥不会发往旧运行地址。导出清单以 UUID 下载 `pdf`、`partial_pdf`、`latex`、`json`，四种产物采用同一页序与已采用修订；元数据说明完整性、质量、错误及缺失页。旧 process/pause、编辑、校准、编译与导出路径作为兼容入口保留。

识别、独立审查和局部修复使用所选协议的 JSON Schema，不自动换模型、改供应商或回退 Chat Completions。HTTP 错误保留状态码和限长的 `message`、`param`、`code`，已知密钥脱敏，不公开原始响应；400、认证及 Schema 配置错误集中停止无效调用。首次正常页面请求发现服务能力问题，不另发付费预检。

后端依赖新增 `mistune`，仅用于旧数据转换。启动器在真实启动前按依赖指纹及缺失模块判断是否安装 `backend/requirements.txt`。此前依赖补全已在项目 `.venv` 安装 `mistune 3.3.4`，其余后端依赖均满足，前端按锁文件同步并移除旧 Markdown 依赖，本机 TinyTeX 及模板宏包、字体已安装，详情见 [LaTeX 排版说明](docs/LATEX_LAYOUT.md#旧数据迁移与实现状态)；该次依赖补全未启动或重启服务，未执行数据库迁移或实际 PDF 编译。以上是历史记录，不代表本轮已执行一键依赖准备。

迁移时先停止识别和服务，再复制项目及数据目录。完整副本中的 `.venv`、`frontend/node_modules` 和 `.cache/launcher` 属于旧电脑环境；换电脑后先将这些目录改名保留，再双击 `ocr.bat` 重建。若使用自定义数据目录，需要单独复制该目录并恢复环境变量。

U 盘副本建议先复制到新电脑本地磁盘后使用。它包含项目文件，但不包含系统安装的 Python、Node.js 或 pnpm，不是免安装版。

## 目录与文档

```text
backend/                API、模型客户端、页面处理、存储及测试
frontend/               React 自动任务总览、结果、可选编辑及 PDF 展示
scripts/                Windows 启停管理、启动前依赖准备及启动器测试
docs/                   使用、配置、架构及验收说明
examples/               历史导出格式示例
ocr.bat                 Windows 统一入口
```

- [操作说明与故障处理](docs/usage.md)
- [旧功能与配置参考](docs/REFERENCE.md)
- [旧模块与接口参考](docs/module-architecture.md)
- [LaTeX 正文、PDF 编译与整书排版](docs/LATEX_LAYOUT.md)
- [最新：第二轮 OCR 重构 Goal（待实施）](docs/OCR_V2_GOAL.md) · [当前问题与新流程设计](docs/OCR_V2_PLAN.md)
- [第二轮编码计划与代理指南](docs/OCR_V2_IMPLEMENTATION.md) · [第二轮进度](docs/OCR_V2_PROGRESS.md)
- [历史：第一轮无人值守重构 Goal](docs/WORKFLOW_REDESIGN_GOAL.md) · [历史产品规格](docs/WORKFLOW_REDESIGN_PLAN.md)
- [历史编码计划](docs/WORKFLOW_REDESIGN_IMPLEMENTATION.md) · [历史静态审查条件](docs/WORKFLOW_REDESIGN_ACCEPTANCE.md) · [历史进度](docs/WORKFLOW_REDESIGN_PROGRESS.md)
- [原书版式还原与 PDF 渲染修复规格](docs/RENDERING_REPAIR_PLAN.md) · [修复验收报告](docs/RENDERING_REPAIR_VERIFICATION.md)
- [历史渲染 Goal 执行入口](docs/RENDERING_REPAIR_GOAL.md) · [历史执行进度](docs/RENDERING_REPAIR_PROGRESS.md)
- [页侧识别与打印装订版](docs/PRINT_LAYOUT.md)
- [历史页面代理设计](docs/PAGE_AGENT_DESIGN.md)
- [验收清单及历史记录](docs/acceptance.md)
- [发布说明、验证记录和打包步骤](docs/RELEASE.md)

## 当前限制

- 仅支持 PDF、PNG、JPEG；单个上传文件上限 100 MB，不支持 EPUB/MOBI 原生导入。
- OCR 质量取决于模型与原图；自动复核和有限修复不能保证准确率，无法确定的区域或页面自动保留源图并标为带问题结果。复杂图形的保留不等于文字化或完整复刻。
- 自动几何和字体使用有来源说明的估计；无法充分恢复时保留源内容。高级工具可主动调整布局及源码，兼容语段没有独立编辑表单。还原与现有模板检查单源页是否意外续页，完整自定义文档可对应多张输出页。
- 不包含账户体系、多用户权限或公网服务安全配置，按本地单用户用途运行。
- 历史 11 页隔离参考的几何与人工核对结论、G1—G7 状态见[旧修复验收报告](docs/RENDERING_REPAIR_VERIFICATION.md)和[旧执行进度](docs/RENDERING_REPAIR_PROGRESS.md)。该次没有真实付费 OCR，固定响应和校准样本不证明新版识别精度。

本轮无人值守版本未测试、未运行验证；不提供实测准确率、通过率或速度结论。[验收清单](docs/acceptance.md) 与 [修复验收报告](docs/RENDERING_REPAIR_VERIFICATION.md) 中的测试、编译和浏览器记录属于旧流程历史。

## 发布与许可

使用 Git 提交生成干净的源码包，避免直接压缩含运行数据的工作目录。具体命令见 [发布说明](docs/RELEASE.md)。仓库当前未指定开源许可证；如需按开源项目发布，应由作者先确定许可证。

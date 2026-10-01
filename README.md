# 纸页重排 · 电子书 OCR

将 PDF、PNG、JPEG 交给视觉模型逐页转录为 LaTeX，人工校对后重新排版，导出 LaTeX 源文件、PDF 和 JSON。适合个人整理扫描书籍、讲义及含公式的资料。

这是 **Windows 优先的本地单用户应用**，由 Python / FastAPI 后端和 React / TypeScript 前端组成。识别使用你自行配置的 OpenAI Responses 或 Google Gemini 原生 API；首次安装依赖和调用模型需要网络。当前源码版本为 `0.1.0`。

## 主要功能

- 一个项目导入多个 PDF 或图片，按文件或单页排序、筛选和穿插编排；新页面默认全部勾选，只有勾选页进入识别、校对与导出。
- 普通内容页保存 LaTeX 正文、公式和表格；页眉页脚与封面封底书目保持结构化纯文本。
- 默认每个项目并发识别 10 页，支持暂停、失败保留旧结果及单页重新识别；可选实验性上下文复用。
- 识别过程中可校对其他页面；保存的人工结果受到保护。
- 支持 A4、A5、A6、B5、B6 和 6 × 9 英寸纸型，可保存字体、字号、行距、首行缩进、段距和页边距；有足够页侧信息时可选择镜像装订边。
- 单页可预览未保存草稿，整书按已保存内容生成实际 PDF；下载 LaTeX、PDF 或 JSON。PDF 由本机 XeLaTeX 编译。

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
4. 在“设置”中选择协议，填写 API 地址、模型和密钥并保存；“测试连接”会调用模型，可能产生用量。
5. 新建项目 → 上传并确认文件 → 勾选所需页、调整页序并确认编排 → 识别未完成页 → 校对保存 → 保存整书排版 → 生成/更新 PDF 与导出。

真正启动服务前，启动器检查依赖指纹和可用模块：`backend/requirements.txt` 变化或后端模块缺失（含旧数据迁移所需 `mistune`）时，仅在项目 `.venv` 中安装 Python 依赖；`frontend/package.json`、`pnpm-lock.yaml` 变化或 Vite 缺失时，执行非交互的 `pnpm install --frozen-lockfile`。成功后在 `.cache/launcher/` 记录依赖指纹，依赖齐全且指纹未变时直接复用，无需联网。Python、Node.js 和 pnpm 仍需预先准备，启动器不会安装这些运行时。已有服务正常运行时直接打开页面，不改动其依赖；需要更新时使用 `ocr.bat Restart`。

LaTeX 准备先使用有效的 `EBOOK_OCR_XELATEX`，未配置或配置路径失效时依次查 PATH 和 Windows 常规 TinyTeX 目录；缺少编译器才下载并校验官方 TinyTeX 包，缺少模板宏包或 Fandol 字体时使用 TeX Live 的 `tlmgr` 补齐。所选目录和变量只在启动进程中设置并由后端继承，不修改系统 PATH 或用户级配置。显式编译器配置无效时警告并继续自动查找；下载失败或无法补齐依赖时停止启动并显示错误。完整查找顺序、自定义发行版与缓存行为见 [LaTeX 排版说明](docs/LATEX_LAYOUT.md#编译环境)。

直接运行后端只负责查找编译器，不下载 TeX 或宏包；缺少 XeLaTeX 时仍可识别、校对并导出 LaTeX 和 JSON，PDF 操作会显示依赖错误。此前默认路径与子进程 PATH 兼容性修复只作静态审查，修复后曾重载后端。本轮一键依赖准备仅作静态阅读、编辑与审查，未执行安装、测试、构建、类型检查、文档编译或服务启动/重启。

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

开发检查命令如下（前端测试需要 Node.js 22.18+）。本次未运行或改写测试；部分现有测试仍引用已移除的 Markdown 字段或渲染文件，尚未适配 LaTeX 契约，以下命令不是本次通过记录。

```powershell
# 在项目根目录
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest backend/tests -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-launcher.ps1

cd frontend
pnpm test
pnpm run build
```

`pnpm run build` 输出到 `frontend/dist/`。当前启动入口使用本地 Vite 服务；仓库没有提供互联网服务器部署或打包为独立 EXE 的方案。

## 数据保存与迁移

默认数据目录为 `backend/data/`，包含 SQLite 数据库、源文件、页面缓存和识别结果。可以用 `EBOOK_OCR_DATA_DIR` 指定其他目录。API 密钥以明文保存在数据库的独立凭据表中；**完整项目备份可能包含密钥和个人书籍，不要把完整备份当作公开发布包。**

从旧版升级后，下一次后端启动会将已有 Markdown 正文一次性转为 LaTeX。迁移写入前保存同目录的 `app-before-latex.db`，原文留在数据库内部 `pages.legacy_markdown` 供恢复；不对 API 或导出公开。迁移在事务中完成，不重新识别，保留校对结果、用量、状态和页序。备份同样可能含凭据，不能放入公开源码包。旧数据转换后的公式、表格和脚注仍需人工校对。

后端依赖新增 `mistune`，仅用于旧数据转换。启动器在真实启动前按依赖指纹及缺失模块判断是否安装 `backend/requirements.txt`。此前依赖补全已在项目 `.venv` 安装 `mistune 3.3.4`，其余后端依赖均满足，前端按锁文件同步并移除旧 Markdown 依赖，本机 TinyTeX 及模板宏包、字体已安装，详情见 [LaTeX 排版说明](docs/LATEX_LAYOUT.md#旧数据迁移与实现状态)；该次依赖补全未启动或重启服务，未执行数据库迁移或实际 PDF 编译。以上是历史记录，不代表本轮已执行一键依赖准备。

迁移时先停止识别和服务，再复制项目及数据目录。完整副本中的 `.venv`、`frontend/node_modules` 和 `.cache/launcher` 属于旧电脑环境；换电脑后先将这些目录改名保留，再双击 `ocr.bat` 重建。若使用自定义数据目录，需要单独复制该目录并恢复环境变量。

U 盘副本建议先复制到新电脑本地磁盘后使用。它包含项目文件，但不包含系统安装的 Python、Node.js 或 pnpm，不是免安装版。

## 目录与文档

```text
backend/                API、模型客户端、页面处理、存储及测试
frontend/               React 界面、LaTeX 校对与 PDF 展示及测试
scripts/                Windows 启停管理、启动前依赖准备及启动器测试
docs/                   使用、配置、架构及验收说明
examples/               历史导出格式示例
ocr.bat                 Windows 统一入口
```

- [操作说明与故障处理](docs/usage.md)
- [详细功能与配置参考](docs/REFERENCE.md)
- [当前模块与接口说明](docs/module-architecture.md)
- [LaTeX 正文、PDF 编译与整书排版](docs/LATEX_LAYOUT.md)
- [页侧识别与打印装订版](docs/PRINT_LAYOUT.md)
- [历史页面代理设计](docs/PAGE_AGENT_DESIGN.md)
- [验收清单及历史记录](docs/acceptance.md)
- [发布说明、验证记录和打包步骤](docs/RELEASE.md)

## 当前限制

- 仅支持 PDF、PNG、JPEG；单个上传文件上限 100 MB，不支持 EPUB/MOBI 原生导入。
- OCR 质量取决于模型与原图，公式、复杂表格和跨页内容需要人工校对。页眉页脚暂不提供界面编辑。
- 重排不保证复刻原书版面，也不保证一个源页对应一张打印纸；长正文可自然续页，复杂表格和图形仍有局限。
- 不包含账户体系、多用户权限或公网服务安全配置，按本地单用户用途运行。
- 本次 LaTeX 迁移仅做静态阅读与代码修改，未运行测试、构建、编译、真实识别或 GUI 验证；历史验证记录不能视为新版已通过验收。

## 发布与许可

使用 Git 提交生成干净的源码包，避免直接压缩含运行数据的工作目录。具体命令见 [发布说明](docs/RELEASE.md)。仓库当前未指定开源许可证；如需按开源项目发布，应由作者先确定许可证。

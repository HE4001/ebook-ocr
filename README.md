# 纸页重排 · 电子书 OCR

将 PDF、PNG、JPEG 交给视觉模型逐页识别，人工校对后重新排版，导出可离线阅读的 HTML、Markdown 和 JSON。适合个人整理扫描书籍、讲义及含公式的资料。

这是 **Windows 优先的本地单用户应用**，由 Python / FastAPI 后端和 React / TypeScript 前端组成。识别使用你自行配置的 OpenAI Responses 或 Google Gemini 原生 API；首次安装依赖和调用模型需要网络。当前源码版本为 `0.1.0`。

## 主要功能

- 一个项目导入多个 PDF 或图片，按文件或单页排序、筛选和穿插编排。
- 普通内容页保留正文、公式、表格及结构化页眉页脚；封面和封底独立提取书目信息。
- 默认每个项目并发识别 10 页，支持暂停、失败保留旧结果及单页重新识别；可选实验性上下文复用。
- 识别过程中可校对其他页面；保存的人工结果受到保护。
- 支持 A4、A5、A6、B5、B6 和 6 × 9 英寸纸型，预览及导出使用相同排版；有足够页侧信息时可选择镜像装订边。
- 导出自包含 HTML、JSON；Markdown 可通过后端导出接口获取。需要 PDF 时在浏览器中打印。

## 快速开始（Windows）

准备以下环境，并确保命令可用：

| 环境 | 要求 |
| --- | --- |
| Python | 3.11 或以上，包含 venv / pip |
| Node.js | 20.19+（20.x）或 22.12+；开发测试建议 22.18+ 或 24.x |
| pnpm | 本次验证使用 11.19.0；依赖以 `frontend/pnpm-lock.yaml` 为准 |
| 模型服务 | 可接收图片并支持结构化输出的 Responses 或 Gemini 模型，以及相应 API Key |

1. 将源码解压到电脑的本地文件夹。
2. 双击根目录 **`ocr.bat`**，按回车启动。首次运行会创建 `.venv` 并按需安装依赖。
3. 健康检查通过后，浏览器打开 `http://127.0.0.1:5173`。
4. 在“设置”中选择协议，填写 API 地址、模型和密钥并保存；“测试连接”会调用模型，可能产生用量。
5. 新建项目 → 上传并确认文件 → 编排页面并确认 → 识别未完成页 → 校对保存 → 预览与导出。

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

运行检查（前端测试需要 Node.js 22.18+）：

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

迁移时先停止识别和服务，再复制项目及数据目录。完整副本中的 `.venv`、`frontend/node_modules` 和 `.cache/launcher` 属于旧电脑环境；换电脑后先将这些目录改名保留，再双击 `ocr.bat` 重建。若使用自定义数据目录，需要单独复制该目录并恢复环境变量。

U 盘副本建议先复制到新电脑本地磁盘后使用。它包含项目文件，但不包含系统安装的 Python、Node.js 或 pnpm，不是免安装版。

## 目录与文档

```text
backend/                API、模型客户端、页面处理、存储及测试
frontend/               React 界面、排版与导出及测试
scripts/                Windows 启停管理与启动器测试
docs/                   使用、配置、架构及验收说明
examples/               历史导出格式示例
ocr.bat                 Windows 统一入口
```

- [操作说明与故障处理](docs/usage.md)
- [详细功能与配置参考](docs/REFERENCE.md)
- [当前模块与接口说明](docs/module-architecture.md)
- [页侧识别与打印排版](docs/PRINT_LAYOUT.md)
- [页面代理设计](docs/PAGE_AGENT_DESIGN.md)
- [验收清单及历史记录](docs/acceptance.md)
- [发布说明、验证记录和打包步骤](docs/RELEASE.md)

## 当前限制

- 仅支持 PDF、PNG、JPEG；单个上传文件上限 100 MB，不支持 EPUB/MOBI 原生导入。
- OCR 质量取决于模型与原图，公式、复杂表格和跨页内容需要人工校对。页眉页脚暂不提供界面编辑。
- 重排不保证复刻原书版面，也不保证一个源页对应一张打印纸；打印分页和数学字体效果取决于浏览器。
- 不包含账户体系、多用户权限或公网服务安全配置，按本地单用户用途运行。
- 自动化验证使用模拟模型响应，未证明真实供应商兼容性、识别精度或全部 GUI 场景已通过验收。

## 发布与许可

使用 Git 提交生成干净的源码包，避免直接压缩含运行数据的工作目录。具体命令见 [发布说明](docs/RELEASE.md)。仓库当前未指定开源许可证；如需按开源项目发布，应由作者先确定许可证。

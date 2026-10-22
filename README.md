# 电子书 OCR / 纸页重排

本项目将 PDF 或图片中的印刷内容识别为可编辑内容，并生成固定模板排版的 PDF、LaTeX 等输出。前端使用 React/Vite，后端使用 FastAPI、SQLite 和 XeLaTeX。

## Windows 安装要求

- Windows、Windows PowerShell 5.1，允许运行项目的 PowerShell 脚本。
- Python 3.11 或更新版本，包含 `venv` 和 `ensurepip`；本地依赖版本取自 Python 3.12.14 环境的安装元数据。
- Node.js 20.19+（20 系列）或 22.12+，以及能读取 pnpm v9 lockfile 的 pnpm（建议 pnpm 10）。将 Python、Node.js 和 pnpm 加入 PATH。启动器也会尝试当前用户 Codex runtime 目录中的工具，但独立安装不依赖它。
- XeLaTeX、`kpsewhich`、中文字体和相关 TeX 包。可预装 TeX Live/TinyTeX；启动器会寻找编译器、尝试安装/修复 TinyTeX 和缺失包，并执行内置中文、数学、表格编译探测。因此首次启动需要联网、写入权限和一定等待时间；非 TeX Live 发行版缺包时需自行安装。
- 可访问所选模型服务及软件包下载源。模型需支持图片输入和项目要求的结构化内容输出。

依赖版本从已有 `frontend/pnpm-lock.yaml` 和本地 `.venv/Lib/site-packages/*.dist-info/METADATA` 静态抄录，未进行兼容性验证。前端直接依赖和 lock importer 均固定版本；后端直接及 Windows 运行时传递依赖均写入 `backend/requirements.txt`，不包含本地测试工具。该清单面向 Windows，不代表跨平台完整锁文件；TeX 工具及包不由这些清单锁定。

## 启动与停止

在项目根目录的 PowerShell 中执行：

```powershell
.\ocr.bat
.\ocr.bat Start
.\ocr.bat Start -NoBrowser
.\ocr.bat Status
.\ocr.bat Stop
.\ocr.bat Restart
```

不传参数时显示交互菜单。`ocr.bat` 调用 `scripts/ocr.ps1`；也可直接执行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\ocr.ps1 Start -NoBrowser`。底层启动和停止脚本分别是 `scripts/start.ps1`、`scripts/stop.ps1`。

启动器创建/复用根目录 `.venv`，按依赖清单同步后端，使用 `pnpm install --frozen-lockfile` 同步前端，并准备 XeLaTeX。前端地址为 [http://127.0.0.1:5173/](http://127.0.0.1:5173/)，后端为 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)；前端 `/api` 代理到后端。端口被其他程序占用时需先处理占用。启动器会进行服务就绪请求；这属于用户运行脚本时的行为，本次修改未执行这些脚本。

关闭菜单或浏览器不会停止后台服务。先在界面暂停任务并等待停止，再执行 `Stop`；停止或重启服务会中断仍在识别的任务。启动日志在 `.cache/launcher/` 下的 `backend.out.log`、`backend.err.log`、`frontend.out.log` 和 `frontend.err.log`；LaTeX 安装日志在 `.cache/dependency-downloads/`。

## 模型配置与使用

打开“设置”，选择 OpenAI Responses 或 Google Gemini 原生协议，填写包含协议和 API 版本的 API 根地址、密钥，并选择或填写识别模型。自定义接口路径可在高级设置中填写。获取模型列表、连接测试和识别都会访问配置的服务。切换协议或根地址后应为当前接入重新填写密钥；协议和根地址都相同时，密钥留空可保留已保存值。保存后再开始任务。

主流程为添加 PDF/图片、选择页面、保存选择、发起识别、查看/编辑内容和生成输出。新流程使用 `printed_fixed_layout`：识别印刷内容，按统一模板排版，一张源页可自然生成多张输出页；它不承诺重现原页几何位置。应人工复核识别文本、公式、未决内容和图形标记后再使用输出。

历史记录中的 `printed_original_only`、原页布局及其他历史布局路径仍保留，用于读取、继续处理和导出既有结果。历史布局校准、源页坐标/区域及原页保真输出属于这些历史流程，不是固定排版的必经步骤，也不能据此推断固定排版具备原页布局保真能力。

## 数据位置与环境变量

默认持久数据位于 `backend/data/`：`app.db` 保存设置、任务、结果和凭据，`books/` 保存书籍源资料、页面资源及输出。数据库迁移会按实际迁移需要生成备份，备份也应视为敏感数据。

可在启动前设置环境变量，随后从同一 PowerShell 启动：

```powershell
$env:EBOOK_OCR_DATA_DIR = 'D:\EbookOcrData'
$env:EBOOK_OCR_XELATEX = 'C:\texlive\2026\bin\windows\xelatex.exe'
.\ocr.bat Start
```

`EBOOK_OCR_DATA_DIR` 覆盖整个数据目录，建议使用绝对路径；`EBOOK_OCR_XELATEX` 指定编译器路径（启动器也会读取用户级设置）。修改后需重启服务。不要把 API 密钥写入这些脚本或提交到仓库。

API 密钥保存在原 SQLite `credentials` 表中，使用 Windows DPAPI 当前用户范围加密，并非 Windows Credential Manager。启动迁移会将旧明文值改为加密值。换电脑或换 Windows 用户恢复数据库后，原密文通常无法解密；应用仍可启动，但需在设置中重新输入密钥。迁移不会自动删除旧备份，也不保证清除数据库空闲页、WAL 或历史文件中残留的旧明文，应自行妥善保管和处理这些文件。

## 停机备份与恢复

1. 在界面暂停任务、等待任务停止，然后执行 `.\ocr.bat Stop`，确保本项目的其他手动启动进程也已停止。
2. 复制完整数据目录到备份位置，而不是只复制 `app.db`。保留 `books/`、数据库、可能存在的 `app.db-wal`/`app.db-shm` 以及迁移备份，并妥善限制访问权限。另行保留对应版本源码和依赖清单。
3. 恢复时保持服务停止；先将现有数据目录另存，再把备份完整复制到目标数据目录。若使用新路径，在启动前设置 `EBOOK_OCR_DATA_DIR`。不要将不同备份的数据库和资源目录混用。
4. 使用对应版本程序启动。恢复的历史任务、模型服务配置和输出应由用户复核；跨电脑或 Windows 用户恢复后重新输入 API 密钥。

本次仅进行了源码阅读和修改，未运行安装、测试、构建、类型检查、lint、编译、应用启动或 API 探测。

Not tested; no runtime validation performed.

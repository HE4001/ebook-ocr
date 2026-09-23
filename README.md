# 电子书识别与重排

这是一个本地单用户工具：把 PDF、PNG 或 JPEG 页面交给独立的页面代理逐页忠实转录，经过人工校对后生成可阅读的 HTML、Markdown 和结构化 JSON。HTML 可以在浏览器中打印为 PDF。

第一版只接受 PDF、PNG、JPEG。EPUB/MOBI 原生导入留到后续版本；不要把它们改名后上传来绕过类型检查。

## 运行环境

- Python 3.11 或更新版本，以及 `backend/requirements.txt` 中列出的依赖。
- Node.js 20.19 或更新版本（或 22.12 及更新版本），以及 pnpm。当前锁文件中的 Vite 8.3.0 和 `@vitejs/plugin-react` 6.1.1 要求 Node `^20.19.0` 或 `>=22.12.0`；仓库用 `pnpm-lock.yaml` 固定前端依赖。
- 可访问所配置 Responses API 的网络连接和一个可用的页面代理模型。项目不会自动切换到 Chat Completions。

后端默认只监听 `127.0.0.1`。前端 Vite 配置固定使用本机的 `127.0.0.1:5173`，并把 `/api` 代理到 `127.0.0.1:8000`。

## 启动

Windows 可以直接双击根目录的 `start.bat`。脚本会从项目目录启动，不受当前终端目录影响：自动验证 Python 3.11+、Node.js 20.19+（或 22.12+）、pnpm 和前端依赖，在项目内创建 `.venv` 并按需安装后端依赖，然后以隐藏窗口启动 `127.0.0.1:8000` 和 `127.0.0.1:5173`，等待健康检查通过后打开浏览器。日志和已启动进程记录位于 `.cache/launcher/`，该目录已被忽略。

需要无浏览器冒烟启动时运行 `start.bat -NoBrowser`。运行中的服务用根目录 `stop.bat` 停止；停止脚本只会停止启动记录中且仍能核验为本项目的进程。若 8000 或 5173 已被其他程序占用，启动脚本会报出端口和 PID，不会结束其他程序。

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

## 第一次使用

1. 启动前后端并打开前端页面。
2. 在设置中填写 API 根地址、Responses 接入路径、页面代理模型、模型推理程度、最大输出 token 数和超时；默认根地址是 `https://api.openai.com/v1`，路径是 `/responses`。模型推理程度（`reasoning_effort`）可直接手动输入，留空表示不发送 `reasoning` 参数并使用服务默认值；填写后识别请求和连接测试都会发送 `reasoning: {"effort":"填写的值"}`。应用不限制枚举值，实际支持情况由服务和模型决定。
   设置页的“填入 DeepSeek 配置”只修改当前草稿，不保存也不发请求：地址填为 `https://api.deepseek.com`、路径为 `/responses`、模型为 `deepseek-flash`、推理程度为 `high`、最大输出 token 数为 `12000`。API 密钥不会自动更换；填入 DeepSeek 密钥后仍需手动保存并测试。页面识别请求使用 base64 PNG `input_image`，模型直接返回 Markdown，不发送 JSON Schema。
3. 首次打开时模型名称和 API 密钥为空是正常状态。用户必须自行选择并填写可用的页面代理模型和密钥；应用不替用户选定某个供应商或模型。旧的分类模型字段可以保留但不参与新流程。保存后的密钥写入默认 `backend/data/app.db` 中独立的 SQLite 凭据记录，按本机明文保存（不提供加密或 keyring）；GET 设置、导出 JSON、浏览器 `localStorage` 和日志都不应包含它。后端重启会从该记录加载，之后不必重复输入。
4. 主动点击“测试连接”确认配置可用。测试使用已保存的最大输出 token 数，只发文本请求，不代表图像识别质量；测试不会替用户开始处理书籍。
5. 导入 PDF、PNG 或 JPEG，等待页面栅格化完成后开始处理。处理时可点“暂停处理”；当前页请求完成并保存结果后停止后续页面，状态变为“已暂停”。点击“继续处理”会跳过已完成页。暂停不会中断已发出的模型请求，该页的用量仍会计入。
6. 逐页查看页面代理返回的 Markdown，检查标题、公式、表格、脚注和页眉页脚；修改后保存页面，运行中的页面不能保存校对。
7. 在排版预览中确认结果，再下载 HTML、Markdown 或 JSON。需要 PDF 时使用浏览器打印功能。书库中的每本书都可以单独删除；删除前需要确认，书籍运行中时删除按钮会禁用。

## 配置与数据

设置字段、页面和用量字段见 [接口与模块说明](docs/module-architecture.md) 及根目录的 [ARCHITECTURE.md](ARCHITECTURE.md)。API 根地址必须是 `http` 或 `https`，不能带用户信息、查询串或片段；Responses 接入路径可留空，此时直接向 API 根地址发送请求，也可填写任意合法的自定义相对路径。`reasoning_effort` 是可选的自由文本，不做枚举校验；空字符串不发送 `reasoning`，非空值按 `reasoning: {"effort": value}` 传给识别与连接测试请求，是否可用由服务和模型决定。`max_output_tokens` 为正整数，默认 `12000`，是本应用的默认额度而非服务上限；页面识别和文本连接测试都使用它，额度包含推理和正文。应用数据、SQLite 文件、源 PDF、单页 PDF 和页面 PNG 由后端保存到 `backend/data/`，也可通过 `EBOOK_OCR_DATA_DIR` 改到本地其他目录。

每页代理使用独立的一次模型请求和独立上下文；这是应用内部的模型请求，不会创建 Codex 任务，也不会携带其他页面的聊天历史。模型直接返回排版好的页面 Markdown，应用保存为页面文本，下载整书 Markdown 时按页合并为 `.md` 文件。旧设置中的 `structured_output` 字段保留兼容，但不再影响识别请求。模型响应是不可信输入，系统会检查响应状态和文本长度；失败页可以重试，成功页会复用。`usage` 只记录供应商实际返回的 token 字段，缺失字段保持 `null`；重试会累计已知用量，无法确认的请求会让 `complete` 为 `false`。重启时运行中的任务会标记为 `interrupted`；已暂停状态会保留。

API 密钥只写入本机 SQLite 的独立凭据表，使用明文存储；它不会写入单独的密钥文件、浏览器存储、日志或书籍导出。空白或未提交密钥会保留已有值，明确清除才会删除记录。升级到支持持久化密钥的版本时，需先重启新版后端，并把旧进程中仍在使用的密钥重新输入并保存一次；保存后后续重启无需再次填写。请按本机数据库访问权限保护 `backend/data/app.db`。

## 已知范围

- 仅支持 PDF、PNG、JPEG 导入；EPUB/MOBI 是后续工作。
- 导出包括 Markdown、结构化 JSON 和自包含 HTML。PDF 由浏览器打印得到，后端不承诺提供 PDF 排版引擎。
- 默认逐页串行处理，每页使用独立上下文，不自动合并跨页段落，也不自动删除页眉、脚注或页码；页眉、页脚和页码以语义标记保留在页面文本中。
- 可从书库单独删除一本书；删除会移除该书记录、页面记录、请求用量记录以及源文件和页面文件。正在处理的书籍返回冲突并由界面禁用删除操作；删除不会影响其他书籍、全局设置或 API 密钥。
- 保持通用 Responses API 的非流式请求，不自动切换 Chat Completions，也不引入 provider 框架。若输出被截断，应提高 `max_output_tokens` 或降低推理程度；已返回的 usage 仍会计入。DeepSeek 的 mock 验证不能代替真实服务联调。
- 公式保存为 LaTeX；前端导出用 KaTeX 生成 MathML 并以 `trust=false` 渲染，避免依赖网络字体。现代浏览器和系统数学字体会影响离线显示效果；解析失败时保留原始公式文本供校对。
- 印刷 CSS 只会尽量避免标题、公式和表格被拆分，不承诺所有浏览器都能实现出版级分页；复杂跨页表格和跨页段落需要人工校对。
- 简单表格按页面 Markdown 保存；复杂合并表格、图表和插图保留可见文字、标签和图注，不根据图形猜测数据或重绘图表。
- 这是本地单用户工具，不包含账号、多用户权限、后台队列、Redis、Celery、WebSocket、插件系统或通用工作流引擎。

更多操作步骤、故障处理和人工校对要点见 [操作说明](docs/usage.md)；模块边界和验收清单见 [架构模块说明](docs/module-architecture.md) 与 [验收清单](docs/acceptance.md)。可用的固定契约示例在 [examples/sample-export.json](examples/sample-export.json)。

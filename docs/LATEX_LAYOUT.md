# LaTeX 正文与 PDF 排版

当前内容页识别、人工校对和排版统一使用 LaTeX。模型返回 `body_latex`，后端存为 `Page.text`；它只包含正文片段。封面封底书目、页眉与页脚仍是结构化纯文本，由整书模板转义后排版。下载格式为 LaTeX、PDF 和 JSON，不继续提供 Markdown 编辑、预览、导出或独立 HTML 下载。

## 使用流程

1. 确认上传、勾选与页序后，识别所需页面。
2. 在正文编辑区校对 LaTeX，或在封面封底书目栏校对纯文本字段。
3. 点击“更新本页 PDF”预览当前页面草稿。该操作使用已保存整书排版，不保存页面修改；确认内容后仍需点击保存校对。
4. 在整书预览保存纸型与排版设置，按需开启“打印装订版”。
5. 点击“生成/更新 PDF”，检查按已保存内容生成的整书 PDF，再下载 LaTeX、PDF 或 JSON。

PDF 只在用户点击更新时编译。草稿、已保存内容、纸型、排版、页序或装订选项变化后，旧预览会失效，需要重新更新。整书生成与 LaTeX 下载使用已保存内容，未保存的页面草稿只参与本页预览。排版草稿需先保存或恢复，才能生成整书 PDF 或下载 LaTeX。失败信息会显示在预览区，不把旧 PDF 当成最新结果。

## 编译环境

PDF 预览与下载需要 XeLaTeX、`ctexbook`、Fandol 字体，以及模板所需宏包。模板使用 `geometry`、`amsmath`、`amssymb`、`longtable`、`array`、`fancyhdr`、`ulem` 和 `hyperref`。可使用包含这些组件的 TeX Live 或 MiKTeX 环境；Windows 一键启动负责准备编译依赖，直接运行后端只查找编译器，不下载发行版或宏包。

一键入口仍是双击 `ocr.bat` 后按回车，或执行 `ocr.bat Start` / `ocr.bat Restart`，无需额外安装菜单。`scripts/start.ps1` 在真实启动服务前调用 `scripts/latex-dependencies.ps1` 的 `Initialize-LatexDependencies`。已有服务正常运行时 `Start` 直接返回，不修改运行中服务的依赖；`Restart` 停止服务后重新准备。

编译器按以下顺序选择：

1. 非空的 `EBOOK_OCR_XELATEX`。启动器在当前进程未设置该变量时读取用户级值；已有当前进程值优先。该变量只接收可执行文件名或路径，不接收附加参数或 shell 命令。一键启动遇到显式配置无效时显示警告并继续自动查找；准备成功后以找到的有效路径替换启动进程中的配置。直接启动后端仍会拒绝无效的显式配置。
2. 未配置或配置无效时，在原有进程 PATH 中查找 `xelatex`。
3. Windows 依次查找 `%APPDATA%\TinyTeX\bin\windows`、`%PROGRAMDATA%\TinyTeX\bin\windows`、`%APPDATA%\TinyTeX\bin\win32`、`%PROGRAMDATA%\TinyTeX\bin\win32` 中的 `xelatex.exe`，未提供相应环境变量时跳过该根目录。

一键启动未找到编译器时，先查配置路径及默认目录中的 `tlmgr.bat`，重装 XeTeX、LaTeX 基础程序、PDF 驱动和路径查询工具，并执行 XeTeX 安装后配置。无法修复时，从官方 GitHub 最新发布获取 Windows TinyTeX-1 包，按官方资产文件名缓存到 `.cache/dependency-downloads/` 并校验发布哈希，使用隐藏进程解压到唯一缓存目录后复制安装，保留下载与解压缓存。安装目标优先 `%APPDATA%\TinyTeX`，必要时使用 `%PROGRAMDATA%\TinyTeX`；目标完整路径必须为 ASCII 且无空格。已有但缺失编译器的目标在新包解压成功后移至同级 `TinyTeX.backup-<唯一编号>`，保留原文件，再安装新副本。没有合适路径、网络失败、哈希不符或解压失败时停止启动并显示错误。

选定编译器后，启动器检查 XeTeX 和 `xdvipdfmx` 程序，并用同目录的 `kpsewhich.exe` 查询模板宏包、Fandol 字体与 XeLaTeX 格式文件；缺少查询工具时自动补装。宏包、字体或程序缺少时使用所选 TeX Live 的 `tlmgr` 针对性安装或重装；管理器明确要求先更新自身时才执行 `tlmgr update --self`。格式文件 `xelatex.fmt` 缺失时补齐 `fmtutil-sys` 并通过 `--byfmt xelatex` 重建。

最后每次准备都会编译脚本内固定的 `latex-dependency-probe.tex`，覆盖宋体、黑体、楷体、仿宋、数学公式、表格、页眉和链接，确认能输出非空 PDF。发现间接缺失的 LaTeX 文件时，通过 `tlmgr search --global --file` 查询所属包并安装，最多补全 20 轮；同一文件补装后仍缺失或出现其他编译错误时停止并保留诊断日志。测试页和 PDF 保存在 `.cache/dependency-downloads/latex-check-*`，不读取或编译用户书籍。完整环境不会访问网络。其他发行版若未提供 `tlmgr` 且组件缺失，需通过其管理器补齐；完整环境可直接使用。工具调用有超时限制，依赖检查或试编译失败时不会启动后端。本流程补齐项目所需组件及其传递依赖，不安装 TeX Live 的所有无关宏包。

选定编译器目录会加入启动进程 PATH，`EBOOK_OCR_XELATEX` 设为所选绝对路径，后端继承它们；不修改系统 PATH 或用户级配置。配置有误时，修正或清除相应进程及用户级变量后重新启动后端，使用启动器时可用 `ocr.bat Restart`。

通过 `uvicorn` 等手动入口启动后端后，编译 API 仍按显式配置、PATH、Windows 常规 TinyTeX 目录查找编译器，其他平台只使用显式配置与 PATH。后端只在编译子进程 PATH 中加入选定编译器的绝对父目录，供同目录 `xdvipdfmx` 等工具使用；不安装任何 TeX 依赖。缺少编译器不阻止手动后端进行 OCR、校对、LaTeX 或 JSON 导出，PDF 操作会显示依赖错误。

此前默认路径与子进程 PATH 兼容性修复只作静态审查，修复后曾重载后端。本轮一键依赖准备仅作静态阅读、编辑与审查，未执行安装、测试、构建、类型检查、实际文档编译、模型调用或服务启动/重启。

后端用 `ctexbook` 与 Fandol 生成统一文档，源文件采用 UTF-8，可在具备相同依赖的环境中离线编译。编译调用不经过 shell，使用 `-no-shell-escape`，Windows 进程隐藏；每次编译进程上限为 120 秒。正文只接受应用支持的命令与环境，拒绝导言区、宏定义、任意文件读写和外部资源命令。命令范围检查不会自动修复 TeX 排版错误，缺括号、表格列数不匹配或不成立的公式仍会成为可见失败。

## 正文片段与校对

正文中不要填 `\documentclass`、`\usepackage`、`\begin{document}` 或 `\end{document}`。字体、纸张和模板由应用管理。编辑区提供“居中”“左对齐”“右对齐”“无缩进”“段落分隔”“公式”按钮，选择文字后可插入对应语法。

| 内容 | 写法 |
| --- | --- |
| 段落 | 段落之间空一行，普通印刷折行合并为连续文本 |
| 原文标题 | `\section*{原文标题}`、`\subsection*{原文标题}`；可见编号放在标题文字中 |
| 对齐 | `center`、`flushleft`、`flushright` 环境 |
| 不缩进 | 段落前使用 `\noindent` |
| 强调 | `\textbf{文字}`、`\textit{文字}`、`\underline{文字}`，只保留原页实际强调 |
| 行内公式 | `\(x^2 + y^2\)` |
| 独立公式 | `\[...\]` 或 `equation*` 环境；可见原编号可用 `\tag{原编号}` |
| 引用与列表 | `quote`、`itemize`、`enumerate`、`description`；有序项用 `\item[原序号]` 保留印刷编号 |
| 表格 | 简表用 `tabular`，长表用 `longtable`；列用 `l`、`c`、`r`，单元格以 `&` 分隔、行末 `\\` |
| 代码 | 行内 `\texttt{转义后的代码}`，原样多行使用 `verbatim` |
| 脚注 | 正文标号用 `\textsuperscript{原标号}`，注释文字按原标号留在该源页正文末尾 |

印刷脚注不使用自动编号的 `\footnote`，不猜测归属或重新挂接。图注、表注保留可见文字和标号，不生成原书没有的标题、编号、图片或目录。数学表达式使用 LaTeX 数学模式；公式中不可读的局部写 `\text{[无法辨认]}`。正文不可读的局部写 `[无法辨认]`，后加手写批注不转录。

普通文字中的特殊字符需要转义：`#`、`$`、`%`、`&`、`_`、`{`、`}` 分别写成 `\#`、`\$`、`\%`、`\&`、`\_`、`\{`、`\}`；字面反斜杠用 `\textbackslash{}`，波浪号用 `\textasciitilde{}`，脱字符用 `\textasciicircum{}`。数学模式与 `verbatim` 遵守各自语法。不要把 Markdown 标题、表格或美元公式定界符当作新正文语法。

例如，原页确有以下标题、公式和脚注时，可校对为：

```latex
\section*{一、原文标题}

这是原页正文，包含行内公式 \(E=mc^2\) 和原脚注标号\textsuperscript{1}。

\[
  \frac{a+b}{c}
\]

\noindent\textsuperscript{1} 原页脚注文字。
```

完整受支持集合以 [backend/latex_content.py](../backend/latex_content.py) 为准，识别约定见 [backend/prompts.py](../backend/prompts.py)。允许集合涵盖常用文字格式、列表、表格和 `amsmath` / `amssymb` 公式，并不接受任意 LaTeX 工程或自定义宏。宽表、复杂合并单元格、图形和跨页内容仍需人工校对；图形只保留可辨文字、标签和图注。

## 整书排版设置

纸型保存在 `Book.paper_size`，其他设置保存在 `Book.layout`，应用于本页 PDF、整书 PDF 与 LaTeX 下载。选择纸型后立即保存，排版表单点击“保存排版”后生效，不调用模型，也不改变页面内容、用量或页序。

| 字段 | 默认值 | 允许值 / 单位 |
| --- | --- | --- |
| `font_family` | `songti`（宋体） | `songti`、`heiti`（黑体）、`kaiti`（楷体） |
| `font_size_pt` | `null`，随纸型 | 6–48 pt；界面留空为 `null` |
| `line_height` | 1.6 | 1–3，基础基线距离为字号 × 此倍率 |
| `paragraph_indent` | 2 | 0–8 个汉字宽度 |
| `paragraph_spacing_pt` | 0 | 0–48 pt |
| `margin_mm` | `null`，随纸型 | 2–50 mm；界面留空为 `null` |

界面中的 pt 对应 LaTeX 的 bp（1/72 英寸）。模板以字号乘以行距倍率设置正文基础基线距离，不再叠乘 `ctex` 默认行距；标题、公式等特殊内容仍有各自排版间距。首行缩进按当前中文字符宽度计算。显式字号和边距在切换纸型后保留，只有留空的设置跟随纸型默认值。

| 纸型 | `paper_size` | 宽 × 高（mm） | 默认字号（pt） | 默认边距（mm） |
| --- | --- | --- | --- | --- |
| A4 | `a4` | 210 × 297 | 12 | 18 |
| A5 | `a5` | 148 × 210 | 11 | 14 |
| A6 | `a6` | 105 × 148 | 10.5 | 10 |
| B5（ISO） | `b5` | 176 × 250 | 11.5 | 16 |
| B6（ISO） | `b6` | 125 × 176 | 11 | 12 |
| 6 × 9 英寸 | `trade_6x9` | 152.4 × 228.6 | 11 | 14 |

封面按标题、署名和出版信息分层排版，封底书目靠底；内容页的页眉页脚保留整页左、中、右锚点、绝对行号和字形。缺失行保持空白，原书页码保持原文，不添加输出页码。源页之间分隔，长文本可以自然续页，不保证一个源页对应一张输出纸。

## 左右页与装订版

普通内容页只根据当前原图的非空页脚判断 `page_side`。有原书页码时，以整组页码相对整页中轴的位置为优先依据；没有实质页码时，才看其他页脚是否一致在同一侧。居中、冲突、无非空页脚或证据不明确时为 `unknown`。不能从页码奇偶、文件名、源页号、编排顺序、页眉或相邻页推断，封面封底始终为 `unknown`。完整识别规则见 [左右页与打印版](PRINT_LAYOUT.md)。

装订选项仅在正文页有非空页脚且 `page_side` 为 `left` 或 `right` 时应用。已知左页的右侧、已知右页的左侧为内侧，边距取当前基础边距的 1.2 倍，外侧取 0.8 倍，两侧总宽度不变。未知页侧、无非空页脚和封面封底使用对称边距。

LaTeX `geometry` 按源页侧别固定移边，长文本续页沿用该源页的边距，不按输出物理页码奇偶切换，不插入凑左右面的空白页，不强制源页落到某个物理左页或右页。装订选项是本次输出选择，不写入 `Book.layout` 或 JSON，也不改变正文、页侧或用量。打印生成的 PDF 时使用其自身纸张尺寸。

## 导出、缓存与接口

JSON 保持 `{book,files,pages}` 结构，`book` 携带纸型、排版及 `content_format: latex`，`pages[].text` 为 LaTeX 正文片段，来源和结构化书目、页眉页脚保留。LaTeX 导出是完整 `.tex` 文档，PDF 下载只在当前整书成功生成后可用。

| 方法与路径 | 用途 |
| --- | --- |
| `PUT /api/books/{id}/layout` | 提交 `{paper_size?: PaperSize, layout?: LayoutSettings}`，至少一项，前端提交完整排版对象；返回 `Book` |
| `GET /api/books/{id}/export` | 下载结构化 JSON |
| `GET /api/books/{id}/export.tex?print_version=false` | 下载已保存整书 LaTeX，不调用编译器 |
| `POST /api/books/{id}/compile?print_version=false` | 编译已保存整书，返回 `{pdf_url}` |
| `POST /api/books/{id}/pages/{number}/compile?print_version=false` | 以 `PageUpdate` 请求体预览本页草稿，返回 `{pdf_url}`，不保存页面 |
| `GET /api/books/{id}/compiled/{sha256}.pdf` | 内联读取成功生成的 PDF |

输出使用 `backend/latex_export.py` 的 `build_latex(detail, print_version=False)` 与 `async compile_pdf(source, output_dir)`。缓存按完整源码的 SHA-256 保存在每书目录的 `latex-cache` 下，同一本书串行编译，完全相同的源码复用成功 PDF。本页草稿编译只取当前已选页，不改变状态、尝试次数或用量；已有缓存文件不表示新内容已生成，前端会随当前内容与排版变化更新预览状态。

## 旧数据迁移与实现状态

旧版 Markdown 只作为启动迁移输入。`backend/storage.py` 在写入旧数据库前通过 SQLite backup 保存同一数据目录下的 `app-before-latex.db`；随后在事务内一次性转换正文，设置 `Book.content_format: latex`，原文留在内部 `pages.legacy_markdown` 供恢复，不对 API 或 JSON 公开。迁移不调用模型，不重新识别，保留人工校对、处理状态、用量、尝试次数及页序。备份已存在时不会覆盖；新建的 LaTeX 数据库不需要旧正文转换。

后端新增 `mistune>=3,<4` 仅用于旧格式转换。转换不会证明旧内容正确，也无法保证所有复杂 Markdown 表格、公式、脚注或嵌入格式都能复刻，应对照原页校对。数据库及 `app-before-latex.db` 可能包含明文 API 密钥和个人书籍，不能放入公开源码包。

初次 LaTeX 编码仅进行静态阅读与文件编辑，未安装依赖。此前依赖补全已在项目 `.venv` 安装 `mistune 3.3.4`，其余 `requirements.txt` 依赖均满足；`pnpm install --frozen-lockfile` 成功，移除 121 个不再需要的包及 Markdown 直接依赖。官方 TinyTeX-1 v2026.09（TeX Live 2026）已安装到本机 `C:\Users\David\AppData\Roaming\TinyTeX`，补齐 `ctex`、Fandol、`fancyhdr`、`ulem` 等共 50 个新增包；模板所需宏包和字体仅通过 `kpsewhich` 查询路径确认。该次曾将用户级 `EBOOK_OCR_XELATEX` 设置为该目录下的 `bin/windows/xelatex.exe`，未改全局 PATH；本轮启动脚本只设置进程环境，不写用户级变量。

此前依赖补全仅检查版本与路径，未运行测试、构建、类型检查、实际文档编译、模型调用或 GUI 验证，未启动或重启服务，也未执行数据库迁移。本轮一键依赖准备仅进行静态阅读、编辑与审查，未执行上述运行操作或安装。现有部分测试尚未适配新契约，历史 Markdown / HTML 验收记录不代表当前实现已验证；实际 PDF 排版与识别质量仍待实际使用检查。

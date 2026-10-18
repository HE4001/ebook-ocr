"""One fixed typesetting policy for image recognition and editable output."""
from __future__ import annotations

import re

FIXED_LAYOUT_VERSION = 1
FIXED_MARGIN_MM = 25
FIXED_PARAGRAPH_SKIP_PT = 6
# size, leading, space before, space after, alignment, weight
FIXED_CATEGORY_STYLES = {
    "title": (20, 30, 18, 12, "center", "bold"),
    "heading": (16, 24, 12, 8, "left", "bold"),
    "subheading": (13, 19.5, 10, 6, "left", "bold"),
    "paragraph": (11, 16.5, 0, FIXED_PARAGRAPH_SKIP_PT, "left", "normal"),
    "equation": (11, 16.5, 6, 6, "left", "normal"),
    "table": (11, 16.5, 6, 6, "left", "normal"),
    "list": (11, 16.5, 0, 6, "left", "normal"),
    "caption": (9, 13.5, 3, 6, "left", "normal"),
    "footnote": (9, 13.5, 3, 6, "left", "normal"),
    "header": (9, 13.5, 3, 6, "center", "normal"),
    "footer": (9, 13.5, 3, 6, "center", "normal"),
    "page_number": (9, 13.5, 3, 6, "center", "normal"),
    "code": (10, 15, 3, 6, "left", "mono"),
    "quote": (11, 16.5, 3, 6, "left", "normal"),
}

# Math commands remain open; document configuration, TeX execution, and resource
# access belong exclusively to the program-owned document and asset mapping.
_FORBIDDEN_COMMAND = re.compile(
    r"\\(?:documentclass|usepackage|RequirePackage|LoadClass|input|include|includeonly|"
    r"includegraphics|graphicspath|DeclareGraphicsExtensions|includepdf|InputIfFileExists|IfFileExists|endinput|write|write18|"
    r"openout|closeout|openin|closein|read|readline|ifeof|catcode|mathcode|lccode|uccode|"
    r"sfcode|delcode|endlinechar|escapechar|newlinechar|csname|endcsname|let|futurelet|"
    r"def|gdef|edef|xdef|newcommand|renewcommand|providecommand|DeclareRobustCommand|"
    r"newenvironment|renewenvironment|newif|newcount|newdimen|newskip|newmuskip|newtoks|"
    r"newread|newwrite|newbox|global|globaldefs|advance|multiply|divide|long|outer|protected|expandafter|noexpand|the|toks|"
    r"every\w*|output|shipout|afterassignment|aftergroup|begingroup|endgroup|bgroup|egroup|"
    r"directlua|latelua|special|immediate|shellescape|pdf\w*|XeTeX\w*|font|fontname|"
    r"fontdimen|nullfont|dump|end|bye|loop|repeat|scantokens|ExplSyntaxOn|ExplSyntaxOff|"
    r"makeatletter|makeatother|obeylines|obeyspaces|verb|verbatim|lstinputlisting|"
    r"bibliography|addbibresource|href|url|filecontents|footnote|marginpar|"
    r"geometry|newgeometry|restoregeometry|ctexset|fontspec|usefont|setmainfont|setCJK\w*|setlength|"
    r"addtolength|fontsize|selectfont|linespread|baselinestretch|baselineskip|parskip|parindent|"
    r"textheight|paperwidth|paperheight|hsize|vsize|"
    r"hoffset|voffset|oddsidemargin|evensidemargin|topmargin|headheight|headsep|footskip|"
    r"displaywidth|displayindent|hangindent|hangafter|parshape|mag|vspace|hspace|vskip|hskip|kern|"
    r"resizebox|scalebox|rotatebox|raisebox|"
    r"newpage|clearpage|cleardoublepage|pagebreak|nopagebreak|enlargethispage|"
    r"pagestyle|thispagestyle|section|subsection|subsubsection|chapter|part|"
    r"needspace|Huge|huge|LARGE|Large|large|small|footnotesize|scriptsize|tiny|normalsize)(?![A-Za-z])",
    re.IGNORECASE,
)
_ENVIRONMENTS = {
    "equation", "equation*", "align", "align*", "alignat", "alignat*", "gather", "gather*",
    "multline", "multline*", "displaymath", "math", "aligned", "alignedat", "gathered",
    "split", "array", "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix",
    "smallmatrix", "cases", "tabular", "tabular*", "tabularx", "longtable",
    "itemize", "enumerate", "description", "quote", "quotation",
}
_TOKENS = re.compile(r"\\[A-Za-z]+|\\.|%[^\n]*|[{}]", re.DOTALL)
_ENVIRONMENT = re.compile(r"\\(begin|end)\s*\{([^{}]+)\}")


def validate_fixed_fragment(value: str) -> str:
    """Reject escaping the fixed wrapper or accessing arbitrary local resources."""
    # Balanced local braces make category groups a real document boundary.
    depth = 0
    visible = []
    cursor = 0
    for match in _TOKENS.finditer(value):
        token = match[0]
        if token.startswith("%"):
            visible.append(value[cursor:match.start()])
            cursor = match.end()
            continue
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("LaTeX 片段不能关闭程序的分类包裹器")
    if depth:
        raise ValueError("LaTeX 片段花括号必须配对")
    visible.append(value[cursor:])
    body = "".join(visible)
    # \end is permitted only as the closing token of a known, balanced environment.
    without_ends = _ENVIRONMENT.sub(lambda match: "" if match[1] == "end" else match[0], body)
    if _FORBIDDEN_COMMAND.search(without_ends):
        raise ValueError("LaTeX 正文不能修改固定排版、导言区、宏定义或访问外部资源")
    if re.search(r"\\(?:linewidth|columnwidth|textwidth)(?![A-Za-z])\s*=", body):
        raise ValueError("LaTeX 正文只能引用可用栏宽，不能修改固定模板的宽度")
    if "^^" in body:
        raise ValueError("LaTeX 正文不能使用 TeX 字符编码逃逸")
    stack = []
    for match in _ENVIRONMENT.finditer(body):
        name = match[2]
        if name not in _ENVIRONMENTS:
            raise ValueError(f"LaTeX 正文环境不属于内容范围：{name}")
        if match[1] == "begin":
            stack.append(name)
        elif not stack or stack.pop() != name:
            raise ValueError("LaTeX 正文环境必须在同一内容块内配对")
    if stack:
        raise ValueError("LaTeX 正文环境必须在同一内容块内结束")
    return value


def fixed_layout_prompt() -> str:
    lines = [f"固定模板版本 {FIXED_LAYOUT_VERSION}：沿项目纸型（默认 A4），边距 {FIXED_MARGIN_MM}mm。",
             f"正文自动折行、自然跨输出页；段首缩进2个汉字，段间{FIXED_PARAGRAPH_SKIP_PT}pt；保持原文和语义段落，不还原原书折行、留白、分栏或像素布局。",
             "各类字号/行距/前后间距（pt）及样式："]
    for category, (size, leading, before, after, alignment, weight) in FIXED_CATEGORY_STYLES.items():
        lines.append(f"{category}: {size}/{leading}, 前{before}/后{after}, {alignment}, {weight}")
    lines.extend([
        "只输出各分类的正文 LaTeX 片段，程序按 category 包裹样式。标题只给转义正文，可有 textbf/textit，不输出 section/chapter 命令。",
        r"paragraph 使用普通转义文字和行内数学 \(...\)；一个语义段落为一块，不能按扫描折行逐行返回。",
        r"equation 包含完整显示数学定界符 \[...\] 或 equation*/align*/gather* 等完整环境；有原编号时用 equation*/align* 与 \tag{原编号}，无原编号用不自动编号的环境。长公式可在原语义运算符处用 aligned/multline* 拆行，字符和数学关系不变，不缩放或缩字。程序不再套数学定界符。",
        r"table 包含完整 longtable/tabular/tabularx 环境和原表头/数据/合并关系，可用 booktabs/multirow。短表优先 tabularx{\linewidth} 的 X 列；longtable 用相对 \linewidth 的 p 列给文字自动换行，列宽总和须为单元格间距/竖线留余量；不沿用原书绝对列宽。长表用 longtable，不缩字、缩放、删列或删数据。无法自然排下则注明 uncertainty。不要 table 浮动环境或额外图注。",
        r"list 使用完整 itemize/enumerate/description 环境；保留原印刷标签，enumerate 用 \item[原标签] 避免生成不同编号。quote 可用完整 quote 环境。",
        r"caption/footnote/header/footer/page_number 按原文独立成块，保留原标号，不调用 caption/footnote 宏，不生成页码。",
        r"code 输出转义后的字面源码，用 \texttt{...} 和必要 \\ 保留有意义的代码换行，不使用 verb/verbatim，不执行原代码。",
        "图形单独返回 type=figure、真实 bbox 和可选可见特征说明。图形资源由程序裁切映射，不写路径、includegraphics、替代数据或图形含义。图注另成 caption 块。",
        "不修改字号、字体尺寸、边距、模板长度、分页或定位；表格可只读引用相对栏宽。不返回完整文档、导言区、宏定义、读写/执行命令或外部资源。图表保持阅读位置，不用浮动环境。",
    ])
    return "\n".join(lines)


def fixed_layout_preamble(width_mm: float, height_mm: float) -> str:
    body_size, body_leading, *_ = FIXED_CATEGORY_STYLES["paragraph"]
    lines = [f"% ebook-fixed-layout-v{FIXED_LAYOUT_VERSION}",
             rf"\documentclass[UTF8,fontset=fandol,{body_size}pt]{{ctexart}}",
             r"\usepackage{geometry,graphicx,amsmath,amssymb,mathrsfs}",
             r"\usepackage{longtable,array,tabularx,booktabs,multirow,enumitem}",
             r"\usepackage{bm,needspace}", r"\usepackage[normalem]{ulem}",
             rf"\geometry{{paperwidth={width_mm}mm,paperheight={height_mm}mm,margin={FIXED_MARGIN_MM}mm}}",
             r"\pagestyle{empty}", r"\raggedbottom",
             r"\setlength{\parindent}{2\ccwd}",
             r"\setlength{\parskip}{0pt}",
             r"\setlength{\emergencystretch}{2em}",
             r"\setlist{topsep=0pt,partopsep=0pt,itemsep=3pt,parsep=0pt,leftmargin=2\ccwd}",
             r"\setlength{\LTpre}{0pt}", r"\setlength{\LTpost}{0pt}",
             r"\setlength{\abovedisplayskip}{0pt}", r"\setlength{\belowdisplayskip}{0pt}",
             r"\setlength{\abovedisplayshortskip}{0pt}", r"\setlength{\belowdisplayshortskip}{0pt}",
             r"\begin{document}", rf"\fontsize{{{body_size}pt}}{{{body_leading}pt}}\selectfont"]
    return "\n".join(lines)


def fixed_block_latex(category: str, fragment: str) -> str:
    validate_fixed_fragment(fragment)
    size, leading, before, after, alignment, weight = FIXED_CATEGORY_STYLES[category]
    commands = [rf"\par\addvspace{{{before}pt}}", r"\begingroup",
                rf"\fontsize{{{size}pt}}{{{leading}pt}}\selectfont"]
    if category in {"title", "heading", "subheading"}:
        body_leading = FIXED_CATEGORY_STYLES["paragraph"][1]
        commands.insert(0, rf"\Needspace{{{before + leading + after + 2 * body_leading}pt}}")
    if category != "paragraph":
        commands.append(r"\setlength{\parindent}{0pt}")
    if category == "quote":
        commands.extend((r"\setlength{\topsep}{0pt}", r"\setlength{\partopsep}{0pt}"))
    if alignment == "center":
        commands.append(r"\centering")
    if weight == "bold":
        commands.append(r"\bfseries")
    elif weight == "mono":
        commands.append(r"\ttfamily")
    commands.extend((fragment, r"\par\endgroup", rf"\addvspace{{{after}pt}}"))
    return "\n".join(commands)

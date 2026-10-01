"""Plain-text escaping and the supported, non-executable LaTeX body syntax."""

from __future__ import annotations

import re


_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
    "%": r"\%", "&": r"\&", "_": r"\_", "#": r"\#", "$": r"\$",
    "^": r"\textasciicircum{}", "~": r"\textasciitilde{}",
}


def escape_latex(text: str) -> str:
    return "".join(_ESCAPES.get(character, character) for character in text)


ALLOWED_ENVIRONMENTS = frozenset("""
    center flushleft flushright quote quotation itemize enumerate description
    verbatim tabular longtable array equation* align* alignat* gather* multline*
    aligned alignedat gathered split matrix pmatrix bmatrix Bmatrix vmatrix
    Vmatrix smallmatrix cases
""".split())

ALLOWED_COMMANDS = frozenset("""
    begin end section subsection subsubsection paragraph subparagraph
    textbf textit emph textrm textsf texttt textnormal textup textsl textsc
    underline sout textsuperscript textsubscript textbackslash textasciicircum
    textasciitilde textbar textless textgreater textbraceleft textbraceright
    textendash textemdash textquotedblleft textquotedblright textquoteleft
    textquoteright textbullet textcopyright textregistered texttrademark
    par noindent indent newline linebreak pagebreak clearpage newpage item null
    raggedright raggedleft centering normalfont rmfamily sffamily ttfamily
    bfseries mdseries itshape upshape slshape scshape songti heiti kaishu
    tiny scriptsize footnotesize small normalsize large Large LARGE huge Huge
    setlength parindent parskip ccwd linewidth textwidth baselineskip
    dimexpr relax tabcolsep arrayrulewidth
    fontsize selectfont hspace vspace hfill vfill rule hrulefill dotfill
    makebox parbox shortstack strut verb
    hline cline multicolumn arraybackslash tabularnewline
    endfirsthead endhead endfoot endlastfoot
    frac dfrac tfrac cfrac binom dbinom tbinom sqrt left right middle
    big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr
    overline underline widehat widetilde hat tilde bar vec dot ddot dddot
    ddddot breve check acute grave mathring overrightarrow overleftarrow
    overleftrightarrow underbrace overbrace overset underset stackrel
    text mbox mathrm mathbf mathit mathsf mathtt mathnormal mathcal mathbb
    mathfrak boldsymbol pmb operatorname substack displaystyle textstyle
    scriptstyle scriptscriptstyle ensuremath phantom hphantom vphantom smash
    tag notag nonumber intertext
    alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota
    kappa varkappa lambda mu nu xi pi varpi rho varrho sigma varsigma
    tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi
    Sigma Upsilon Phi Psi Omega
    sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth
    log ln lg exp lim limsup liminf min max sup inf det gcd Pr arg dim ker
    deg hom mod bmod pmod pod
    sum prod coprod int iint iiint iiiint oint smallint
    limits nolimits infty partial nabla ell hbar imath jmath Re Im wp
    emptyset varnothing aleph beth gimel daleth angle measuredangle triangle
    Box Diamond square blacksquare boxtimes checkmark complement circ bullet cdot
    cdots ldots vdots ddots dots dotsc dotsb dotsm dotsi dotso
    pm mp times div ast star circ odot oplus ominus otimes oslash bigcirc
    cap cup uplus sqcap sqcup vee wedge setminus smallsetminus wr
    bigcap bigcup bigvee bigwedge biguplus bigoplus bigotimes bigodot
    le leq ge geq neq ne equiv approx sim simeq cong asymp propto
    ll gg lesssim gtrsim lessapprox gtrapprox prec succ preceq succeq
    subset supset subseteq supseteq subsetneq supsetneq nsubseteq nsupseteq
    sqsubset sqsupset sqsubseteq sqsupseteq in ni notin owns parallel nparallel
    mid nmid perp models vdash dashv Vdash vDash Vvdash nvdash nvDash
    nVdash nVDash bowtie Join smile frown doteq triangleq fallingdotseq
    risingdotseq therefore because forall exists nexists neg lnot land lor
    top bot not
    to gets mapsto longmapsto hookrightarrow hookleftarrow
    rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow
    longrightarrow longleftarrow longleftrightarrow Longrightarrow
    Longleftarrow Longleftrightarrow uparrow downarrow updownarrow Uparrow
    Downarrow Updownarrow nearrow searrow swarrow nwarrow
    rightleftharpoons leftrightharpoons rightharpoonup rightharpoondown
    leftharpoonup leftharpoondown implies impliedby iff
    langle rangle lvert rvert lVert rVert vert Vert lceil rceil lfloor rfloor
    lbrace rbrace backslash colon quad qquad enspace thinspace negthinspace
""".split())

_HEADINGS = {"section", "subsection", "subsubsection", "paragraph", "subparagraph"}
_SYMBOLS = frozenset("\\{}%&#_$,;:! /()[]|^-~'`=.\n\r\t")
_LENGTH_ARGUMENT = re.compile(r"\{\s*\\(parindent|parskip)\s*\}\s*\{[^{}]*\}")


def validate_latex_fragment(text: str) -> None:
    """Reject file access, macro definitions and encoded control sequences.

    This deliberately checks the supported vocabulary, rather than attempting to
    repair arbitrary TeX. Remaining typesetting errors are reported by XeLaTeX.
    """
    _validate_commands(text, ALLOWED_COMMANDS)


def _argument(text: str, position: int) -> tuple[str, int]:
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != "{":
        raise ValueError("LaTeX 环境名称必须写在花括号内")
    end = text.find("}", position + 1)
    if end < 0 or "{" in text[position + 1:end]:
        raise ValueError("LaTeX 环境名称格式不正确")
    return text[position + 1:end], end + 1


def _validate_commands(text: str, allowed_commands: frozenset[str]) -> None:
    # TeX treats both CR and LF as line ends, including the end of a comment.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "^^" in text:
        raise ValueError("LaTeX 正文不允许 ^^ 编码；字面脱字符请使用 \\textasciicircum{}")
    if any(ord(character) < 32 and character not in "\n\r\t" for character in text):
        raise ValueError("LaTeX 正文包含不支持的控制字符")
    environments: list[str] = []
    position = 0
    while position < len(text):
        character = text[position]
        if character == "%":
            newline = text.find("\n", position)
            position = len(text) if newline < 0 else newline + 1
            continue
        if character != "\\":
            position += 1
            continue
        position += 1
        if position == len(text):
            raise ValueError("LaTeX 正文末尾存在未完成的反斜杠命令")
        start = position
        while position < len(text) and text[position].isascii() and text[position].isalpha():
            position += 1
        if position == start:
            if text[position] not in _SYMBOLS:
                raise ValueError("LaTeX 正文包含不支持的控制符")
            position += 1
            continue
        command = text[start:position]
        if command not in allowed_commands:
            raise ValueError(f"LaTeX 正文不支持命令 \\{command}")
        if command in _HEADINGS:
            if text[position:position + 1] != "*":
                raise ValueError("LaTeX 标题须使用带 * 的无编号命令，原书编号保留在标题文字中")
        elif command == "setlength":
            if not _LENGTH_ARGUMENT.match(text, position):
                raise ValueError("正文只允许设置 \\parindent 或 \\parskip")
        elif command == "verb":
            if text[position:position + 1] == "*":
                position += 1
            if position == len(text) or text[position].isspace() or text[position].isalpha():
                raise ValueError("\\verb 缺少有效的字面文本分隔符")
            end = text.find(text[position], position + 1)
            if end < 0 or "\n" in text[position + 1:end] or "\r" in text[position + 1:end]:
                raise ValueError("\\verb 的字面文本必须在同一行闭合")
            position = end + 1
        elif command in {"begin", "end"}:
            environment, position = _argument(text, position)
            if environment not in ALLOWED_ENVIRONMENTS:
                raise ValueError(f"LaTeX 正文不支持环境 {environment}")
            if command == "end":
                if not environments or environments.pop() != environment:
                    raise ValueError("LaTeX 环境的 begin/end 不匹配")
            elif environment == "verbatim":
                ending = r"\end{verbatim}"
                end = text.find(ending, position)
                if end < 0:
                    raise ValueError("verbatim 环境缺少结束标记")
                position = end + len(ending)
            else:
                environments.append(environment)
    if environments:
        raise ValueError(f"LaTeX 环境 {environments[-1]} 缺少结束标记")

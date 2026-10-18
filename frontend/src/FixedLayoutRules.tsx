import type { FixedContentCategory, RecognitionScope } from './types'

export const CATEGORY_LABEL: Record<FixedContentCategory, string> = {
  title: '标题', heading: '一级标题', subheading: '二级标题', paragraph: '正文',
  equation: '公式', table: '表格', list: '列表', caption: '图表注', footnote: '脚注',
  header: '原页页眉', footer: '原页页脚', page_number: '原页页码', code: '代码', quote: '引用',
}

export function scopeLabel(scope: RecognitionScope | undefined) {
  return scope === 'printed_fixed_layout' ? '印刷内容 · 固定排版'
    : scope === 'printed_original_only' ? '历史任务 · 印刷原文与旧排版'
      : '历史任务 · 全可见内容范围'
}

export function FixedLayoutRules() {
  return <section className="fixed-layout-rules" aria-label="固定排版规则">
    <h3>固定排版规则 · 版本 1</h3>
    <p>整页图像直接交给识别模型，按阅读顺序分类并输出 LaTeX 片段。程序统一应用下列样式，正文自动折行、自然分页。</p>
    <dl>
      <dt>纸张与正文</dt><dd>使用项目纸型，四边 25 mm；正文 11 pt、行距 16.5 pt，段首缩进 2 字、段间 6 pt。</dd>
      <dt>标题层级</dt><dd>标题 20 pt、居中粗体；一级标题 16 pt、二级标题 13 pt，均为粗体。标题前预留后续正文空间，减少孤立在页底的标题。</dd>
      <dt>公式、表格、列表</dt><dd>正文大小，保留内容结构及公式编号。长公式可在关系或运算符处换行，不改变原式；表格列宽按正文宽度安排，单元格可换行，保留全部列和数据，不缩字或缩放。图表按内容顺序放置。</dd>
      <dt>图注、脚注与页边文字</dt><dd>9 pt、行距 13.5 pt；原页页眉、页脚和页码居中放在内容顺序内。图形经独立核验后使用干净资源，资源不可用时保留占位和原因。</dd>
      <dt>代码与引用</dt><dd>代码 10 pt、等宽、行距 15 pt；引用使用正文大小。</dd>
    </dl>
    <p>只处理印刷内容；后加手写批注排除，遮挡或无法辨认的内容单独说明。每张源页按上述规则排版，可自然跨多个输出页，再按所选顺序合并；原图用于内容对照。</p>
  </section>
}

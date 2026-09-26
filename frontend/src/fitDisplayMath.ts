// Keep this function self-contained: the standalone HTML embeds the same implementation.
export function installDisplayMathFit(root: HTMLElement): () => void {
  const formulas = Array.from(root.querySelectorAll<MathMLElement>('math[display="block"]')).map((math) => {
    const wrapper = math.parentElement!
    const originalFontSize = wrapper.style.fontSize
    const originalWidth = wrapper.style.width
    wrapper.classList.add('math-fit-display')
    return { math, wrapper, originalFontSize, originalWidth }
  })
  if (!formulas.length) return () => {}

  let active = true
  let frame = 0
  let printing = window.matchMedia('print').matches

  function fit() {
    for (const { math, wrapper, originalFontSize, originalWidth } of formulas) {
      wrapper.style.fontSize = originalFontSize
      wrapper.style.width = originalWidth
      let available = wrapper.getBoundingClientRect().width
      if (available <= 0) continue
      const fontSize = parseFloat(getComputedStyle(wrapper).fontSize)
      const parentFontSize = parseFloat(getComputedStyle(wrapper.parentElement!).fontSize)
      let printFontRatio = 1

      if (printing) {
        const page = wrapper.closest<HTMLElement>('.book-page')!
        const style = getComputedStyle(page)
        const widthMm = parseFloat(style.getPropertyValue('--paper-width-mm'))
        const marginMm = parseFloat(style.getPropertyValue('--paper-margin-mm'))
        const fontMm = parseFloat(style.getPropertyValue('--paper-font-mm'))
        const currentContentWidth = page.getBoundingClientRect().width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
        // beforeprint can fire before print media layout; use the selected paper's real text area.
        available *= (widthMm - marginMm * 2) * 96 / 25.4 / currentContentWidth
        printFontRatio = fontMm * 96 / 25.4 / parseFloat(style.fontSize)
      }

      // Native block MathML fills its parent even when its mtable is wider.
      wrapper.style.width = 'max-content'
      const naturalWidth = Math.max(wrapper.getBoundingClientRect().width, math.scrollWidth) * printFontRatio
      wrapper.style.width = originalWidth
      if (naturalWidth > available) {
        let scale = Math.max(0, available - 1) / naturalWidth
        wrapper.style.fontSize = `${fontSize / parentFontSize * scale}em`
        // At most three corrections cross native MathML's font/spacing rounding steps.
        for (let correction = 0; correction < 3; correction++) {
          wrapper.style.width = 'max-content'
          const fittedWidth = Math.max(wrapper.getBoundingClientRect().width, math.scrollWidth) * printFontRatio
          wrapper.style.width = originalWidth
          if (fittedWidth <= available) break
          scale *= Math.min(.98, Math.max(0, available - 2) / fittedWidth)
          wrapper.style.fontSize = `${fontSize / parentFontSize * scale}em`
        }
      }
    }
  }

  function schedule() {
    if (!active) return
    cancelAnimationFrame(frame)
    frame = requestAnimationFrame(fit)
  }

  function beforePrint() {
    printing = true
    cancelAnimationFrame(frame)
    fit()
  }

  function afterPrint() {
    printing = false
    schedule()
  }

  const widths = new WeakMap<Element, number>()
  const observer = new ResizeObserver((entries) => {
    let changed = false
    for (const entry of entries) {
      if (widths.get(entry.target) !== entry.contentRect.width) changed = true
      widths.set(entry.target, entry.contentRect.width)
    }
    if (changed) schedule()
  })
  new Set(formulas.map(({ wrapper }) => wrapper.parentElement!)).forEach((element) => observer.observe(element))
  window.addEventListener('beforeprint', beforePrint)
  window.addEventListener('afterprint', afterPrint)
  document.fonts.addEventListener('loadingdone', schedule)
  void document.fonts.ready.then(schedule)
  fit()

  return () => {
    active = false
    cancelAnimationFrame(frame)
    observer.disconnect()
    window.removeEventListener('beforeprint', beforePrint)
    window.removeEventListener('afterprint', afterPrint)
    document.fonts.removeEventListener('loadingdone', schedule)
    for (const { wrapper, originalFontSize, originalWidth } of formulas) {
      wrapper.style.fontSize = originalFontSize
      wrapper.style.width = originalWidth
      wrapper.classList.remove('math-fit-display')
    }
  }
}

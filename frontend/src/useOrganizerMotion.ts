import { useLayoutEffect, useRef } from 'react'

type Position = { left: number; top: number }
type Motion = { animation: Animation; x: number; y: number }

export function useOrganizerMotion(layoutKey: string, scopeKey = '') {
  const containerRef = useRef<HTMLElement>(null)
  const previous = useRef(new Map<string, Position>())
  const motions = useRef(new Map<string, Motion>())
  const previousScope = useRef(scopeKey)
  const footerObserver = useRef<ResizeObserver | null>(null)

  useLayoutEffect(() => {
    const container = containerRef.current
    if (!container) return

    if (!footerObserver.current) {
      const footer = container.querySelector<HTMLElement>('.organizer-save-bar')!
      const reserveFooter = () => container.style.setProperty('--organizer-footer-height', `${footer.offsetHeight}px`)
      reserveFooter()
      footerObserver.current = new ResizeObserver(reserveFooter)
      footerObserver.current.observe(footer)
    }

    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const nodes = Array.from(container.querySelectorAll<HTMLElement>('[data-motion-key]'))
    const next = new Map<string, Position>()
    const measured = nodes.map((node) => {
      const key = node.dataset.motionKey!
      // Each list/grid is a positioned parent; transforms and scrolling never enter this baseline.
      const position = { left: node.offsetLeft, top: node.offsetTop }
      next.set(key, position)
      return { node, key, position }
    })

    for (const [key, motion] of motions.current) {
      if (reducedMotion || !next.has(key) || (scopeKey !== previousScope.current && key.startsWith('page:'))) {
        motion.animation.cancel()
        motions.current.delete(key)
      }
    }

    for (const { node, key, position } of measured) {
      const before = previous.current.get(key)
      if (reducedMotion || !before || (scopeKey !== previousScope.current && key.startsWith('page:'))) continue
      let x = before.left - position.left
      let y = before.top - position.top
      if (Math.abs(x) < 1 && Math.abs(y) < 1) continue

      const active = motions.current.get(key)
      if (active) {
        const progress = active.animation.effect!.getComputedTiming().progress ?? 1
        x += active.x * (1 - progress)
        y += active.y * (1 - progress)
        active.animation.cancel()
      }
      const animation = node.animate(
        [{ translate: `${x}px ${y}px` }, { translate: '0px 0px' }],
        { duration: 220, easing: 'cubic-bezier(.2,.8,.2,1)' },
      )
      motions.current.set(key, { animation, x, y })
      animation.onfinish = () => {
        if (motions.current.get(key)?.animation === animation) motions.current.delete(key)
      }
    }
    previous.current = next
    previousScope.current = scopeKey
  }, [layoutKey, scopeKey])

  useLayoutEffect(() => () => {
    motions.current.forEach(({ animation }) => animation.cancel())
    motions.current.clear()
    footerObserver.current?.disconnect()
    footerObserver.current = null
  }, [])

  return containerRef
}

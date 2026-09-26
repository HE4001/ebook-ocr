export type FileParents = Record<string, string | null>

export function flattenFileParents(parents: FileParents): FileParents {
  return Object.fromEntries(Object.entries(parents).map(([id, parent]) => {
    while (parent && parents[parent]) parent = parents[parent]
    return [id, parent]
  }))
}

export function fileSubtree(id: string, order: string[], parents: FileParents): Set<string> {
  const ids = new Set([id])
  for (const file of order) {
    let parent = parents[file]
    while (parent) {
      if (parent === id) { ids.add(file); break }
      parent = parents[parent]
    }
  }
  return ids
}

export function fileDepth(id: string, parents: FileParents): number {
  let depth = 0
  let parent = parents[id]
  while (parent) { depth += 1; parent = parents[parent] }
  return depth
}

export function flattenFiles(order: string[], parents: FileParents): string[] {
  const children = new Map<string | null, string[]>()
  for (const id of order) {
    const parent = parents[id] ?? null
    if (!children.has(parent)) children.set(parent, [])
    children.get(parent)!.push(id)
  }
  const visit = (parent: string | null): string[] => (children.get(parent) ?? []).flatMap((id) => [id, ...visit(id)])
  return visit(null)
}

export function moveFileTree(order: string[], parents: FileParents, id: string, parent: string | null, target?: string, side: 'before' | 'after' = 'after') {
  const subtree = fileSubtree(id, order, parents)
  if ((parent && (parents[parent] || subtree.has(parent))) || (target && subtree.has(target))) return { order, parents }
  const moved = order.filter((file) => subtree.has(file))
  const rest = order.filter((file) => !subtree.has(file))
  const nextParents = { ...parents, [id]: parent }
  if (parent) moved.forEach((file) => { nextParents[file] = parent })
  let index = rest.length
  if (target) {
    const targetTree = fileSubtree(target, rest, nextParents)
    index = side === 'before' ? rest.indexOf(target) : Math.max(...rest.map((file, position) => targetTree.has(file) ? position : -1)) + 1
  } else if (parent) {
    const parentTree = fileSubtree(parent, rest, nextParents)
    index = Math.max(...rest.map((file, position) => parentTree.has(file) ? position : -1)) + 1
  }
  rest.splice(index, 0, ...moved)
  return { order: flattenFiles(rest, nextParents), parents: nextParents }
}

export function insertPageBlock(layout: number[], moved: number[], anchor: number | null, side: 'before' | 'after' = 'after'): number[] {
  const moving = new Set(moved)
  if (anchor !== null && moving.has(anchor)) return layout
  const next = layout.filter((id) => !moving.has(id))
  const index = anchor === null ? next.length : next.indexOf(anchor) + (side === 'after' ? 1 : 0)
  next.splice(index, 0, ...moved)
  return next
}

export function reorderVisiblePages(layout: number[], visible: number[], from: number, to: number): number[] {
  const next = [...visible]
  const [moved] = next.splice(from, 1)
  next.splice(to, 0, moved)
  const included = new Set(visible)
  let index = 0
  return layout.map((id) => included.has(id) ? next[index++] : id)
}

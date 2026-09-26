export type ProcessScope = 'odd' | 'even' | 'custom';

export function resolvePageSelection(
  scope: ProcessScope,
  input: string,
  pageCount: number,
): { pages: number[]; error: string | null } {
  if (scope !== 'custom') {
    const pages: number[] = [];
    for (let page = scope === 'odd' ? 1 : 2; page <= pageCount; page += 2) {
      pages.push(page);
    }
    return {
      pages,
      error: scope === 'even' && pages.length === 0 ? '当前文件没有偶数页' : null,
    };
  }

  if (!input.trim()) {
    return { pages: [], error: '请输入要处理的页码' };
  }

  const pages = new Set<number>();
  for (const part of input.replace(/，/g, ',').split(',')) {
    const segment = part.trim();
    if (!segment) {
      return { pages: [], error: '逗号之间及末尾不能留空' };
    }

    const match = /^(\d+)(?:\s*-\s*(\d+))?$/.exec(segment);
    if (!match) {
      return { pages: [], error: '页码格式错误，请输入整数或范围，如 3,8-12' };
    }

    const start = Number(match[1]);
    const end = match[2] === undefined ? start : Number(match[2]);
    if (start < 1 || end < 1 || start > pageCount || end > pageCount) {
      return { pages: [], error: `页码必须在 1 到 ${pageCount} 之间` };
    }
    if (start > end) {
      return { pages: [], error: '范围起始页不能大于结束页' };
    }

    for (let page = start; page <= end; page += 1) {
      pages.add(page);
    }
  }

  return { pages: [...pages].sort((a, b) => a - b), error: null };
}

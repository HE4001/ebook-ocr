import type { AffineTransform, BBox, BoxBp, PageMapEntry } from './types'

export type PagePoint = { x: number; y: number }

export function transformPoint(point: PagePoint, affine: AffineTransform): PagePoint {
  const [a, b, c, d, e, f] = affine
  return { x: a * point.x + c * point.y + e, y: b * point.x + d * point.y + f }
}

export function sourcePointToOutput(point: PagePoint, mapping: PageMapEntry): PagePoint {
  const output = transformPoint(point, mapping.source_to_output_affine!)
  return { x: output.x / mapping.output_width_bp!, y: output.y / mapping.output_height_bp! }
}

export function outputPointToSource(point: PagePoint, mapping: PageMapEntry): PagePoint {
  const [a, b, c, d, e, f] = mapping.source_to_output_affine!
  const x = point.x * mapping.output_width_bp! - e
  const y = point.y * mapping.output_height_bp! - f
  const determinant = a * d - b * c
  return { x: (d * x - c * y) / determinant, y: (a * y - b * x) / determinant }
}

export function sourceBoxToOutput(box: BBox, mapping: PageMapEntry): BBox {
  const corners = [
    { x: box[0], y: box[1] }, { x: box[2], y: box[1] },
    { x: box[0], y: box[3] }, { x: box[2], y: box[3] },
  ].map((point) => sourcePointToOutput(point, mapping))
  return [Math.min(...corners.map((point) => point.x)), Math.min(...corners.map((point) => point.y)),
    Math.max(...corners.map((point) => point.x)), Math.max(...corners.map((point) => point.y))]
}

export function outputBoxToNormalized(box: BoxBp, mapping: PageMapEntry): BBox {
  return [box[0] / mapping.output_width_bp!, box[1] / mapping.output_height_bp!,
    box[2] / mapping.output_width_bp!, box[3] / mapping.output_height_bp!]
}

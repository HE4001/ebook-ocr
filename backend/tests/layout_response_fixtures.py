"""Original fixed responses; these exercise the contract, not live OCR accuracy."""

from copy import deepcopy


def line_style(**changes):
    return {
        "font_family": None, "font_size_bp": None, "font_size_ratio": 1,
        "bold": None, "italic": None, "basis": "model_estimate", **changes,
    }


def region(region_id, kind="paragraph", bbox=None, order=0):
    return {"region_id": region_id, "kind": kind, "order": order,
            "bbox": bbox, "parent_id": None, "basis": "model_estimate"}


def observed_line(line_id, block_id, text, order, bbox, kind="text", **changes):
    return {"line_id": line_id, "block_id": block_id, "order": order,
            "kind": kind, "latex": text, "bbox": bbox,
            "baseline": bbox[3] - .01, "style": line_style(),
            "basis": "model_estimate", **changes}


def page_response(text="正文", kind="content", version=2, *,
                  header_segments=None, footer_segments=None, cover_fields=None):
    headers = deepcopy(header_segments or [])
    footers = deepcopy(footer_segments or [])
    value = {
        "page_kind": kind, "page_side": "unknown", "cover_fields": deepcopy(cover_fields or []),
        "header_segments": headers, "body_latex": text if kind == "content" else "",
        "footer_segments": footers,
    }
    if version == 1:
        return value
    observation = {"schema_version": 1, "body_frame": [.1, .1, .9, .85],
                   "regions": [], "lines": [], "equation_groups": [], "review_reasons": []}
    if kind == "content":
        if text:
            observation["regions"].append(region("paragraph", bbox=[.1, .1, .9, .85]))
            for index, original_line in enumerate(text.splitlines()):
                y = .15 + index * .07
                observation["lines"].append(observed_line(
                    f"line-{index + 1}", "paragraph", original_line, index,
                    [.1, y, .9, y + .05],
                ))
        for area, segments, y in (("header", headers, .025), ("footer", footers, .9)):
            if segments:
                observation["regions"].append(region(area, area, [.1, y, .9, y + .08]))
            for index, segment in enumerate(segments):
                line_y = y + (segment["row"] - 1) * .025
                observation["lines"].append(observed_line(
                    f"{area}-{index + 1}", area, segment["text"], len(observation["lines"]),
                    [.1, line_y, .9, line_y + .025],
                    "page_number" if segment["kind"] == "page_number" else area,
                    style=line_style(bold=segment["bold"], italic=segment["italic"]),
                ))
        if not observation["lines"]:
            observation["body_frame"] = None
    value.update(response_version=2, layout=observation if kind == "content" else None)
    return value


def original_printed_page():
    """Two original text lines, a three-line equation group and a printed underline."""
    value = page_response()
    observation = value["layout"]
    observation["regions"] = [region("paragraph", bbox=[.1, .1, .9, .3]),
                              region("derivation", "equation", [.2, .35, .85, .62], 1)]
    observation["lines"] = [
        observed_line("text-1", "paragraph", r"普通文字\textbf{局部粗体}与 \(x_1+y_2\)。", 0, [.1, .1, .9, .15]),
        observed_line("text-2", "paragraph", r"原印刷\underline{下划线}保留，续行不合并。", 1, [.15, .2, .9, .25]),
        observed_line("eq-1", "derivation", "u&=a+b", 2, [.2, .35, .8, .4], "equation"),
        observed_line("eq-2", "derivation", "&=c+d", 3, [.3, .45, .8, .5], "equation"),
        observed_line("eq-3", "derivation", "&=e", 4, [.3, .55, .8, .6], "equation"),
    ]
    observation["equation_groups"] = [{
        "group_id": "derivation-1", "line_ids": ["eq-1", "eq-2", "eq-3"],
        "bbox": [.2, .35, .85, .62], "align_x": .3,
        "number": {"latex": "(A)", "line_id": "eq-3", "bbox": [.8, .55, .9, .6], "anchor_x": .85},
        "basis": "model_estimate",
    }]
    observation["review_reasons"] = [
        "后加手写补记、圈画及手画下划线位于 [.88,.18,.98,.3]，不进入正文；与印刷字重叠处待人工核对。",
    ]
    value["body_latex"] = ""
    return value

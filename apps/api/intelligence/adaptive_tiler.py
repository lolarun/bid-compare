"""adaptive_tiler.py — 自适应页面切片，用于 OCR 整页失败时的条件降级。

触发条件（由 table_recognizer 决定，本模块只做切片）：
- thinking-retry 后仍 extracted_rows < expected_rows * 0.7
- html_fallback + extracted_rows == 0 + 页面含价格信号

最小可靠实现：
- 根据宽高比判断横向/纵向（portrait 和 landscape 都用横向条带）
- 条带重叠 10-15%，记录原页比例坐标
- 去重：以 (seq, name[:10], spec[:8]) 为 key，保留先出现的
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image

log = logging.getLogger(__name__)

# 默认参数
DEFAULT_N_TILES = 4
DEFAULT_OVERLAP = 0.12   # 12% 重叠

_PRICE_SIGNALS = ["单价", "合价", "综合单价", "价税合计", "含税", "不含税"]


@dataclass
class TileInfo:
    """单个切片的图片和位置信息。"""
    image_bytes: bytes                              # PNG bytes
    tile_index: int                                 # 0-based，按页面从上到下
    bbox_pct: tuple[float, float, float, float]     # (x0, y0, x1, y1) 占原页比例


def tile_page(
    image_bytes: bytes,
    n_tiles: int = DEFAULT_N_TILES,
    overlap: float = DEFAULT_OVERLAP,
) -> list[TileInfo]:
    """把一页图片切为 n_tiles 条带，各条带之间重叠 overlap 比例。

    方向选择：
    - Landscape（w > h）→ 纵向条带（左→右）：每条带含全部属性行 + 部分物料列。
      适合转置表（行=属性，列=物料），LLM 能看到完整物料信息。
    - Portrait（h >= w）→ 横向条带（上→下）：每条带含全部物料列 + 部分行。
      适合普通纵向表。

    返回按 tile_index 排序（横向按 x，纵向按 y）的切片列表。
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        w, h = img.size
        landscape = w > h
        log.debug(
            "tile_page: image %dx%d → %s %d tiles (overlap=%.0f%%)",
            w, h, "vertical" if landscape else "horizontal", n_tiles, overlap * 100,
        )

        tiles: list[TileInfo] = []

        if landscape:
            # 纵向条带：沿 x 轴切分（适合宽页转置表）
            tile_w = w / n_tiles
            overlap_px = int(tile_w * overlap)
            for i in range(n_tiles):
                x0 = max(0, int(i * tile_w) - overlap_px)
                x1 = min(w, int((i + 1) * tile_w) + overlap_px)
                crop = img.crop((x0, 0, x1, h))
                buf = io.BytesIO()
                crop.save(buf, format="PNG", compress_level=1)
                tiles.append(TileInfo(
                    image_bytes=buf.getvalue(),
                    tile_index=i,
                    bbox_pct=(x0 / w, 0.0, x1 / w, 1.0),
                ))
        else:
            # 横向条带：沿 y 轴切分（适合普通纵向表）
            tile_h = h / n_tiles
            overlap_px = int(tile_h * overlap)
            for i in range(n_tiles):
                y0 = max(0, int(i * tile_h) - overlap_px)
                y1 = min(h, int((i + 1) * tile_h) + overlap_px)
                crop = img.crop((0, y0, w, y1))
                buf = io.BytesIO()
                crop.save(buf, format="PNG", compress_level=1)
                tiles.append(TileInfo(
                    image_bytes=buf.getvalue(),
                    tile_index=i,
                    bbox_pct=(0.0, y0 / h, 1.0, y1 / h),
                ))

        return tiles


def has_price_signal(html: str) -> bool:
    """检查 HTML 是否含价格信号（触发 tiling 的前置条件之一）。"""
    return any(s in html for s in _PRICE_SIGNALS)


def dedup_raw_items(
    all_items: list[dict],
    name_key: str = "name",
) -> list[dict]:
    """去重多条切片 raw_items，以 (seq, name[:10], spec[:8]) 为 key，保留先出现。

    Args:
        all_items: 所有切片合并后的 LLM 输出 items（已按 tile_index 顺序排列）。
        name_key: 名称字段，招标侧为 "name"，报价侧为 "material"。
    """
    seen: set[tuple] = set()
    result: list[dict] = []
    for item in all_items:
        key = _item_dedup_key(item, name_key)
        if key in seen:
            log.debug("dedup_raw_items: skip duplicate key=%s", key)
        else:
            seen.add(key)
            result.append(item)
    log.info(
        "dedup_raw_items: %d → %d after dedup (removed %d overlap duplicates)",
        len(all_items), len(result), len(all_items) - len(result),
    )
    return result


def _item_dedup_key(item: dict, name_key: str) -> tuple:
    seq  = str(item.get("seq") or "").strip()
    name = str(item.get(name_key) or "").strip()[:10]
    spec = str(item.get("spec") or "").strip()[:8]
    # 如果三者都空，用整体 hash 防止所有空行被当成同一行
    if not seq and not name and not spec:
        return ("__empty__", id(item))
    return (seq, name, spec)

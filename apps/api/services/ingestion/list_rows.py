"""list_rows.py — 清单类表格里「这一行是不是一条物料」的共用判据。

design/32 A1 原文写的是 "non-item row filtering, **shared by both sides**"，
但两侧各有一套：招标侧 `tender_list._FOOTER_MARKERS` 是一个 7 词的表尾词表，
报价侧 `quote_confirmation_service._GRAND_TOTAL_NAME_RE` 是一条更窄的正则。
实测代价：凯硕新正那份 PDF 的合计行叫 `含税合价（元）：`，报价侧正则里有
`含税合计` 没有 `含税合价`，**一个字之差漏网**，那 932,154 被当成第 90 条
报价行入库，明细合计翻倍，声明总价闭环门判 fail——用户被挡在门外去修一个
他没制造的问题。招标侧那张词表里恰好有「合价」，本来能拦住。

所以这里只做一件事：**把词表统一到一处**，两侧各按自己的行形状调用。
不做过度抽象——两侧的"行"结构不同（招标是单元格列表，报价是 item dict），
硬套一个通用行模型只会让两边都别扭。

## 为什么判据不能是「没有数量就不是条目」

这是最容易想到、也最危险的写法。同一份文件里 `qty` 为空的两行：

    #89  material='缓闭式止回阀'   unit='EPDM'          金额 3,460.00   ← 真条目
    #90  material='含税合价（元）：' unit='含税合价（元）：' 金额 932,154.00 ← 合计行

#89 是列串位（材质 EPDM 落进了 unit，数量丢了）造成的**识别缺陷**，行本身
是一条真实报价。按"无数量即丢弃"处理，等于把 3,460 元静默删掉，还顺手让
声明总价闭环门更容易通过——**用删行让门通过**，正是 CLAUDE.md「等级不得靠
静默填充或下游猜测抬高」要防的东西。

## 也不能用「金额等于其余行之和」

看着像个自校验的好信号，实测不成立：这份文件前 89 行的含税合价求和是
906,614，合计行写的是 932,154，差 25,540——识别本身丢了值。用它当判据，
在识别质量不完美时反而拦不住合计行（而识别完美时也就不需要它了）。

## 真正用的判据（必须同时满足）

1. **该行没有数量**——有数量的行永远按条目处理，名字叫什么都不改判；
2. 且满足下列任一条**正面证据**：
   - 文本命中表尾词表（`FOOTER_MARKERS`）；
   - 名称/规格/单位三列同值且非空——标签串进了所有文本列，正常条目不可能
     三列一模一样。这条不依赖任何词表，语言无关。

命中的行**不静默丢弃**：调用方要把数量报出来（见 `AggregateRow.reason`），
让"我们排除了一行"这件事在界面上看得见。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 清单表尾常见词。取自 `tender_list._FOOTER_MARKERS`（招标侧沿用多轮的那张
#: 表），报价侧现在共用同一份——两处各维护一份正是本模块要消灭的问题。
FOOTER_MARKERS: tuple[str, ...] = (
    "含税", "合价", "合计", "总计", "小计", "税金", "说明", "备注：",
)


@dataclass(frozen=True)
class AggregateRow:
    """判定为非条目行的结论。`reason` 要能原样给用户看。"""
    index: int
    label: str
    reason: str


def text_hits_footer_marker(text: str) -> bool:
    """这段文本是否像清单表尾的标签。"""
    t = (text or "").strip()
    return bool(t) and any(m in t for m in FOOTER_MARKERS)


def classify_quote_row(
    index: int, *, name: str, spec: str, unit: str, qty: float | None,
) -> AggregateRow | None:
    """报价行 → 非条目结论；是正常条目则返回 None。

    判据见模块文档。**有数量就一定是条目**，这一条优先于其余所有判据。
    """
    if qty is not None:
        return None

    n, sp, u = (name or "").strip(), (spec or "").strip(), (unit or "").strip()

    if n and n == sp == u:
        # 标签串进了所有文本列。语言无关，不依赖词表。
        return AggregateRow(index, n, f"名称/规格/单位三列同为「{n}」，判定为表尾标签行")

    for field_text, field_label in ((n, "名称"), (sp, "规格")):
        if text_hits_footer_marker(field_text):
            return AggregateRow(
                index, field_text,
                f"无数量且{field_label}「{field_text}」命中表尾词，判定为合计/说明行")

    return None

"""vl_snapshot_provider.py — VL-direct 的确定性回放 provider。

与 legacy 的 `snapshot_provider.SnapshotProvider` 同一用途、不同契约：legacy 缓存的是
逐页 OCR HTML 与逐次 LLM JSON；VL-direct 整份一次调用，只需要冻住**两样东西**——
模型返回的原始 CSV，和那一次用的旋转表。

## 为什么 API 级 E2E 必须用它

`ExtractionPipeline.extract_quote` 的 VL 分支条件是
`hasattr(provider, "vl_extract_csv")`。要让端到端测试真的走 VL，就得有一个具备这个
方法、又不打真实 API 的 provider。`MockProvider` 能造合成 CSV，适合测接线；本类回放
**真实文档的真实模型输出**，适合测"这份 PDF 在这条链上会得到什么"。

## 只回放不录制

录制归 `scripts/record_vl_snapshots.py`（它要跑完整的方向预检投票）。本类刻意不提供
record 模式：混进"缺失就打真实 API"的分支，测试就可能在无人察觉时联网并产生费用，
也不再确定。**缺快照就报错。**

用法：
    prov = VLSnapshotProvider.from_slug("quote_cable_hongsheng")
    pipeline = ExtractionPipeline(provider=prov)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "vl_snapshots"

_LABEL = re.compile(r"PAGE_(\d+)_ROT_")


class VLSnapshotProvider:
    """回放一份已录制的 VL 快照。只实现 `vl_extract_csv`，别的一概没有。

    **故意只有这一个方法**：这样 provider 具备什么能力就一目了然，也让
    `extract_quote` 的分支判断落在 VL 上而不会有第二种可能。
    """

    name = "vl_snapshot"

    def __init__(self, snapshot: dict, *, source: str = "<inline>"):
        for key in ("csv", "rotations"):
            if key not in snapshot:
                raise ValueError(f"VL 快照缺字段 {key}：{source}")
        self.snapshot = snapshot
        self.source = source
        self.calls: list[str] = []          # 记录调用序列，测试可断言"确实走了 VL"

    @classmethod
    def from_slug(cls, slug: str, *, snapshot_dir: Path | None = None) -> "VLSnapshotProvider":
        path = (snapshot_dir or SNAPSHOT_DIR) / f"{slug}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"VL 快照缺失：{path}。录制："
                f"python scripts/record_vl_snapshots.py --doc <文档名>"
            )
        return cls(json.loads(path.read_text(encoding="utf-8")), source=str(path))

    # ─── provider 契约 ───────────────────────────────────────────────────────

    def vl_extract_csv(
        self, images: list[bytes], prompt: str, *,
        model: str | None = None, labels: list[str] | None = None, **_kw,
    ) -> str:
        """抽取与方向预检共用此方法，靠 `labels` 区分——与真实 provider 一致。"""
        if labels:
            self.calls.append("orient")
            return self._orient(labels)
        self.calls.append("extract")
        return self.snapshot["csv"]

    def _orient(self, labels: list[str]) -> str:
        """按录制时的旋转表回答。

        **表里没有的页答 0（不用转），而不是不答。** 录制时"判定为 0°"和"没达成
        共识"是两回事，但快照只存了最终采用的旋转表；把缺席一律当"不用转"会把
        当时的"未决"抹成"已决"。故未决页单独记在快照里，由 build_draft 用
        `unresolved_pages` 承载——这里不负责区分，也不假装能区分。
        """
        rot = {int(k): v for k, v in (self.snapshot.get("rotations") or {}).items()}
        pages = sorted({int(m.group(1)) for l in labels if (m := _LABEL.match(l))})
        return "\n".join(f"{p},{rot.get(p, 0)}" for p in pages)

    # ─── 供测试断言 ──────────────────────────────────────────────────────────

    @property
    def unresolved_pages(self) -> list[int]:
        return list(self.snapshot.get("unresolved_pages") or [])

    @property
    def prompt_sha256(self) -> str:
        return self.snapshot.get("prompt_sha256", "")

    def assert_prompt_current(self, current_prompt: str) -> None:
        """快照录于另一版提示词时报错。

        与 test_cable_golden 的重放同一个道理：旧快照是旧格式的输入，拿它验证新
        解析器，测试还绿着但验证的东西已经不存在了。
        """
        import hashlib

        want = hashlib.sha256(current_prompt.encode("utf-8")).hexdigest()[:16]
        if self.prompt_sha256 != want:
            raise AssertionError(
                f"VL 快照录于另一版提示词（{self.prompt_sha256} != {want}）：{self.source}。"
                f"重新录制：python scripts/record_vl_snapshots.py --all"
            )

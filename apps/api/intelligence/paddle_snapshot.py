"""paddle_snapshot.py — PaddleOCR-VL 的确定性回放，供 API 级 E2E 测试。

跟 `vl_snapshot_provider.py`（qwen 时代）同一用途、不同注入点：qwen 的 provider
是一个类实例（`self.provider.vl_extract_csv`），API E2E 测试换掉 `self.provider`
即可拦截。Paddle 走的是模块级函数（`paddle_ocr.submit_and_parse`），报价识别不
再经过 `LLMProvider` 抽象（design/26 P4）——没有 `self.provider` 这个钩子可换，
测试改用 monkeypatch 直接换掉
`apps.api.intelligence.providers.paddle_ocr.submit_and_parse` 本身。

同一个假 `submit_and_parse` 要服务多个供应商（一次测试里依次上传不同供应商的
PDF）。**不能按文件路径匹配供应商**——后端把上传文件存成内容哈希文件名
（`intake.py` 的存储约定），原始文件名（含供应商名）在到达 `submit_and_parse`
时已经不在路径里了，实测复现过。改用跟 qwen 时代的 `_Router.current` 同一个
模式：调用方在每次上传前显式设置 `.current`，`submit_and_parse` 只读这一个
状态，不猜。

只回放，不录制——理由跟 vl_snapshot_provider.py 一致：混进"缺失就打真实 API"
的分支，测试可能在无人察觉时联网产生费用。缺快照就报错。

快照就是真实调用 `apps.api.intelligence.providers.paddle_ocr.submit_and_parse`
落盘的原始 JSON（design/26 P0 的 SHA 绑定产物，或 §6 P2b 验收批跑的产物），
原样复制进 `tests/fixtures/paddle_snapshots/`，不做任何裁剪——裁剪等于让测试
验证一份不存在的输入。
"""
from __future__ import annotations

import json
from pathlib import Path

SNAPSHOT_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "paddle_snapshots"


class PaddleSnapshotReplay:
    """回放已录制的 Paddle 快照。调用前先设置 `.current` 指到哪个供应商的
    关键字，`submit_and_parse` 只读这一个状态。`calls` 记录每次被调用时的
    文件路径，供测试断言"确实被调用了"。
    """

    def __init__(self, slug_by_keyword: dict[str, str], *, snapshot_dir: Path | None = None):
        self._dir = snapshot_dir or SNAPSHOT_DIR
        self._slug_by_keyword = slug_by_keyword
        self.current: str | None = None
        self.calls: list[str] = []

    @classmethod
    def from_slugs(cls, keyword_to_slug: dict[str, str],
                   *, snapshot_dir: Path | None = None) -> "PaddleSnapshotReplay":
        d = snapshot_dir or SNAPSHOT_DIR
        for kw, slug in keyword_to_slug.items():
            path = d / f"{slug}.json"
            if not path.exists():
                raise FileNotFoundError(
                    f"Paddle 快照缺失（关键字={kw!r}）：{path}。复制一份真实识别"
                    f"产物到这个路径（outputs/baidu_paddleocr_vl/ 或 "
                    f"outputs/paddle_p2/ 下同名文档的 .json，或重新跑一次 "
                    f"scripts/p2_acceptance_run.py --doc <文档名> --runs 1）")
        return cls(keyword_to_slug, snapshot_dir=snapshot_dir)

    def submit_and_parse(self, file_path: str, **_kw) -> dict:
        self.calls.append(file_path)
        assert self.current is not None, "未指定当前供应商（.current 没设）"
        slug = self._slug_by_keyword[self.current]
        return json.loads((self._dir / f"{slug}.json").read_text(encoding="utf-8"))

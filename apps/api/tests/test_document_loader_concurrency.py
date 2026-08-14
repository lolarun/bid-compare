"""DocumentLoader 并发契约。

背景：pypdfium2 底层的 PDFium 不是线程安全的。此前 `_PDF_RENDER_SEM` 允许 3 路
并发渲染、`get_page_count` 连信号量都没有，实测在多份文档并行识别时于原生层
触发非法指令（Windows 0xc000001d），且是概率性的——同一批七份跑出过 5/7 崩和
7/7 崩。Python 侧只看得到一个 OSError，无从定位，所以这里用测试把"必须串行"
钉死，而不是靠调并发数碰运气。

夹具用临时生成的 PDF，不依赖 tests/fixtures/documents 下的真实投标文件。
"""
from __future__ import annotations

import concurrent.futures as cf
import threading

import pytest

from apps.api.intelligence import document_loader as dl
from apps.api.intelligence.document_loader import DocumentLoader


@pytest.fixture
def tiny_pdf(tmp_path):
    """三页 PDF。用 pypdfium2 自己造，避免把二进制夹具塞进仓库。"""
    pdfium = pytest.importorskip("pypdfium2")
    doc = pdfium.PdfDocument.new()
    for _ in range(3):
        doc.new_page(200, 300)
    p = tmp_path / "tiny.pdf"
    doc.save(str(p))
    doc.close()
    return str(p)


def test_all_pdfium_entrypoints_hold_the_lock(tiny_pdf, monkeypatch):
    """漏掉任何一个入口都等于没锁——get_page_count 当初就是这么漏的。

    用一个会记录"调用时锁是否被持有"的探针替换 PdfDocument。
    """
    seen: list[tuple[str, bool]] = []
    real = dl.pdfium.PdfDocument

    def _held_by_someone() -> bool:
        """必须从**另一个**线程探测：RLock 可重入，持有者自己再 acquire 一定成功。"""
        out = []

        def probe():
            got = dl._PDF_LOCK.acquire(blocking=False)
            if got:
                dl._PDF_LOCK.release()
            out.append(not got)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return out[0]

    class Probe:
        def __init__(self, path):
            seen.append((caller[0], _held_by_someone()))
            self._d = real(path)

        def __len__(self):
            return len(self._d)

        def __getitem__(self, i):
            return self._d[i]

        def close(self):
            self._d.close()

    monkeypatch.setattr(dl.pdfium, "PdfDocument", Probe)

    caller = [""]
    for name, fn in (
        ("get_page_count", lambda: DocumentLoader.get_page_count(tiny_pdf)),
        ("render_pages", lambda: DocumentLoader.render_pages(tiny_pdf, [1])),
        ("to_images", lambda: DocumentLoader.to_images(tiny_pdf)),
        ("to_thumbnails", lambda: DocumentLoader.to_thumbnails(tiny_pdf)),
    ):
        caller[0] = name
        fn()

    unlocked = [n for n, held in seen if not held]
    assert not unlocked, f"这些入口没有在锁内打开 PdfDocument：{unlocked}"


def test_renders_never_overlap_across_threads(tiny_pdf):
    """真正要防的是"两次渲染在时间上重叠"，而不是"锁对象存在"。"""
    inside = 0
    max_inside = 0
    guard = threading.Lock()
    real = DocumentLoader._render_page_pdfium

    def spy(doc, index0):
        nonlocal inside, max_inside
        with guard:
            inside += 1
            max_inside = max(max_inside, inside)
        try:
            return real(doc, index0)
        finally:
            with guard:
                inside -= 1

    DocumentLoader._render_page_pdfium = staticmethod(spy)
    try:
        with cf.ThreadPoolExecutor(8) as ex:
            out = list(ex.map(
                lambda _: len(DocumentLoader.render_pages(tiny_pdf, [1, 2, 3])),
                range(16)))
    finally:
        DocumentLoader._render_page_pdfium = staticmethod(real)

    assert max_inside == 1, f"检测到 {max_inside} 路渲染重叠，PDFium 会在原生层崩"
    assert out == [3] * 16


def test_concurrency_is_not_configurable():
    """把并发做成 env 开关，等于把"要不要崩"变成可配项。"""
    assert not hasattr(dl, "_PDF_RENDER_SEM")
    assert isinstance(dl._PDF_LOCK, type(threading.RLock()))


def test_lock_is_reentrant():
    """render_pages 持锁期间还要走 _render_page_pdfium；将来若在内层再取锁，
    非重入锁会直接自死锁。"""
    with dl._PDF_LOCK:
        assert dl._PDF_LOCK.acquire(blocking=False)
        dl._PDF_LOCK.release()


def test_concurrent_results_are_identical(tiny_pdf):
    """并发下不只是不崩，产物也必须与串行一致（错页/串页同样是缺陷）。"""
    serial = DocumentLoader.render_pages(tiny_pdf, [1, 2, 3])
    with cf.ThreadPoolExecutor(8) as ex:
        par = list(ex.map(
            lambda _: DocumentLoader.render_pages(tiny_pdf, [1, 2, 3]), range(8)))
    assert all(r == serial for r in par)

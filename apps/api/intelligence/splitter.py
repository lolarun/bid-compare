"""PageSplitter — split a list of page images into fixed-size batches.

For large PDFs we cannot send all pages in a single LLM request:
  - token budget overruns cause truncation or refusal
  - a 12-page PDF may take 60+ s in one shot; smaller batches recover faster

PageSplitter.split() divides pages into consecutive batches. Each batch is
processed independently by the LLM; ResultAggregator then merges the partial
results. First-page context (supplier header, project header) is repeated on
every batch so the model has metadata available on later batches too.
"""

from __future__ import annotations

DEFAULT_BATCH_SIZE = 4   # pages per LLM call (empirically safe for Qwen-VL)
CONTEXT_PAGES = 1        # how many leading pages to prepend as context on tail batches


class PageSplitter:
    """Stateless utility — split pages into overlapping-context batches."""

    @staticmethod
    def split(
        images: list[bytes],
        batch_size: int = DEFAULT_BATCH_SIZE,
        context_pages: int = CONTEXT_PAGES,
    ) -> list[list[bytes]]:
        """Split *images* into batches of at most *batch_size* pages.

        Args:
            images: ordered list of PNG bytes (one per PDF page).
            batch_size: maximum pages per batch (default 4).
            context_pages: number of leading pages to prepend on batches 2+
                so the model can still see the document header / supplier info.

        Returns:
            List of batches; each batch is a list[bytes].
            Single-batch documents (len <= batch_size) return [[all pages]].

        Examples:
            >>> sp = PageSplitter()
            >>> batches = PageSplitter.split(images, batch_size=4, context_pages=1)
            # 12 pages → [[0..3], [0,4..7], [0,8..11]]  (page 0 repeated as context)
        """
        if not images:
            return []

        n = len(images)
        if n <= batch_size:
            return [images]

        ctx = images[:context_pages]          # header page(s) repeated on tail batches
        batches: list[list[bytes]] = []

        for start in range(0, n, batch_size):
            chunk = images[start : start + batch_size]
            if start == 0:
                batches.append(chunk)
            else:
                # prepend context so the model knows which document this belongs to
                batches.append(ctx + chunk)

        return batches

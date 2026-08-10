"""apps.api package init.

Load .env into os.environ as early as possible — BEFORE any submodule's
module-level os.getenv(...) runs. pydantic Settings(env_file=.env) only fills
the Settings object, NOT os.environ, so config read via os.getenv (多 key
DASHSCOPE_API_KEYS、PAGE_CONCURRENCY、OCR_PER_KEY_CONCURRENCY、
EXTRACTION_THREAD_POOL_SIZE) was silently ignored when set only in .env.
override=False → real shell env vars still win.

注：PDF_RENDER_CONCURRENCY 已移除——PDFium 非线程安全，渲染改为全局串行，
并发数不再是可配项（见 intelligence/document_loader.py）。
"""
from pathlib import Path as _Path

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_Path(__file__).resolve().parent / ".env", override=False)
except Exception:
    pass

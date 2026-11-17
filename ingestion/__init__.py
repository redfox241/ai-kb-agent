"""摄入层：vault → frontmatter → chunks → index。"""
from .chunking import build_page_summary, chunk_text
from .frontmatter import clean_text, parse_frontmatter
from .index_service import IndexService, IndexSnapshot
from .vault import scan_vault

__all__ = [
    "build_page_summary",
    "chunk_text",
    "clean_text",
    "parse_frontmatter",
    "IndexService",
    "IndexSnapshot",
    "scan_vault",
]

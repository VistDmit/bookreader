"""Чтение книг: fb2, epub, txt, pdf, docx -> единая модель Book."""

from .loader import SUPPORTED, detect_format, load
from .model import Block, Book, Section

__all__ = ["load", "detect_format", "SUPPORTED", "Book", "Section", "Block"]
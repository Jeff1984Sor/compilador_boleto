"""Utilitarios para manipular PDFs: split por pagina e merge."""

from __future__ import annotations

import io
from typing import Iterable

from pypdf import PdfReader, PdfWriter


def split_por_pagina(pdf_bytes: bytes) -> list[bytes]:
    """Divide um PDF em N PDFs, um por pagina. Retorna lista de bytes."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    paginas: list[bytes] = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        paginas.append(buf.getvalue())
    return paginas


def merge_pdfs(pdfs_bytes: Iterable[bytes]) -> bytes:
    """Mescla varios PDFs (em bytes) em um unico PDF, preservando a ordem."""
    writer = PdfWriter()
    for pdf in pdfs_bytes:
        reader = PdfReader(io.BytesIO(pdf))
        for page in reader.pages:
            writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def contar_paginas(pdf_bytes: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)

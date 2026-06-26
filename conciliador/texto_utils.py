"""Extracao de dados via camada de TEXTO do PDF (pypdf).

A maioria dos boletos/comprovantes digitais (DARE-SP, Itau, Bradesco, BB...)
traz a linha digitavel e o valor como texto selecionavel. Ler o texto e
muito mais preciso e barato do que mandar imagem pro OpenAI Vision - alem de
resolver o caso de "valores iguais", ja que a linha digitavel e unica por titulo.

Quando o PDF e escaneado (sem texto), estas funcoes retornam vazio e o
chamador cai no fallback de visao (openai_client).
"""

from __future__ import annotations

import io
import re

from pypdf import PdfReader, PdfWriter

from .openai_client import DadosTitulo


# Marcadores de que uma PAGINA e um comprovante de pagamento (e nao o boleto).
# Usados para limpar boletos que vem com comprovantes ja grudados no arquivo.
_MARCADORES_COMPROVANTE = (
    "comprovante de pagamento",
    "via contribuinte",
    "autenticacao digital",
    "autenticação digital",
    "pagamento efetuado em",
    "operacao efetuada em",
    "operação efetuada em",
)


def _pagina_eh_comprovante(texto_pagina: str) -> bool:
    t = (texto_pagina or "").lower()
    return any(m in t for m in _MARCADORES_COMPROVANTE)


def manter_pagina_boleto(pdf_bytes: bytes) -> bytes:
    """Reduz um arquivo de BOLETO a uma unica pagina: a do boleto.

    Muitos arquivos chegam com comprovantes (as vezes de outro titulo) grudados.
    O resultado final deve ter so 2 paginas: boleto + comprovante certo. Aqui
    mantemos apenas a 1a pagina que NAO parece comprovante (o boleto). Se nao
    der para distinguir (sem texto), mantem a 1a pagina do arquivo.
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        return pdf_bytes

    if len(reader.pages) <= 1:
        return pdf_bytes

    idx_boleto = 0
    for i, page in enumerate(reader.pages):
        if not _pagina_eh_comprovante(page.extract_text() or ""):
            idx_boleto = i
            break

    writer = PdfWriter()
    writer.add_page(reader.pages[idx_boleto])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def texto_pdf(pdf_bytes: bytes) -> str:
    """Extrai todo o texto do PDF. Retorna '' se nao houver texto (escaneado)."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return ""


# Sequencia de 44/47/48 digitos, possivelmente separada por espacos, pontos e hifens.
# Ex.: "85860000000-4 73000185112-4 60590136345-5 37620260724-0" (DARE, 48 digitos)
#      "23791 62825 50020 207945 35004 663403 1 14890000105378"   (boleto, 47 digitos)
_LINHA_RE = re.compile(r"(?<!\d)(\d[\d.\s-]{42,60}\d)(?!\d)")


def linha_digitavel(texto: str) -> str | None:
    """Acha a primeira linha digitavel valida (44/47/48 digitos) no texto."""
    for linha in texto.splitlines():
        for m in _LINHA_RE.finditer(linha):
            digitos = re.sub(r"\D", "", m.group(1))
            if len(digitos) in (44, 47, 48):
                return m.group(1).strip()
    return None


_MONEY_RE = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")

# Rotulos onde costuma aparecer o valor "de verdade" (e nao R$ 0,00 de juros/multa).
_LABELS_BOLETO = ["valor total", "valor do documento", "valor do boleto", "valor da receita"]
_LABELS_COMPROVANTE = [
    "valor final",
    "valor do pagamento",
    "valor do boleto",
    "valor do documento",
    "valor total",
    "valor:",
]


def valor(texto: str, tipo: str) -> str | None:
    """Extrai o valor monetario priorizando rotulos conhecidos; ignora 0,00."""
    low = texto.lower()
    labels = _LABELS_BOLETO if tipo == "boleto" else _LABELS_COMPROVANTE
    for lab in labels:
        start = 0
        while True:
            idx = low.find(lab, start)
            if idx == -1:
                break
            trecho = texto[idx: idx + 60]
            for m in _MONEY_RE.finditer(trecho):
                if m.group(0) != "0,00":
                    return m.group(0)
            start = idx + len(lab)
    # Fallback: primeiro valor nao-zero no documento inteiro.
    for m in _MONEY_RE.finditer(texto):
        if m.group(0) != "0,00":
            return m.group(0)
    return None


# Rotulos de onde tirar o nome do beneficiario/favorecido (ordem de prioridade).
_LABELS_NOME = [
    "nome do recebedor",
    "beneficiario final",
    "beneficiario",
    "beneficiário",
    "razao social",
    "razão social",
    "favorecido",
    "cedente",
    "nome / razao social",
    "nome / razão social",
]

# Termos que NAO sao nome (quando o rotulo casa com o pagador/devedor, ignoramos).
_RE_NOME = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,&'/-]{3,}")


def beneficiario(texto: str) -> str | None:
    """Extrai o nome do beneficiario/favorecido a partir de rotulos conhecidos."""
    low = texto.lower()
    for lab in _LABELS_NOME:
        idx = low.find(lab)
        if idx == -1:
            continue
        # Pega o trecho logo apos o rotulo (e o ':' eventual) e isola o nome.
        trecho = texto[idx + len(lab): idx + len(lab) + 80]
        trecho = trecho.lstrip(" :\t")
        m = _RE_NOME.match(trecho)
        if m:
            nome = m.group(0).strip(" .,-/")
            if len(nome) >= 4:
                return nome
    return None


def parse(texto: str, tipo: str) -> DadosTitulo:
    """Monta um DadosTitulo a partir do texto extraido."""
    return DadosTitulo(
        linha_digitavel=linha_digitavel(texto),
        valor=valor(texto, tipo),
        beneficiario=beneficiario(texto),
        raw="texto",
    )

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

from pypdf import PdfReader

from .openai_client import DadosTitulo


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

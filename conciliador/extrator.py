"""Extracao hibrida: TEXTO do PDF primeiro, OpenAI Vision como fallback.

Para PDFs digitais (a maioria), le a linha digitavel/valor direto do texto -
preciso, gratis e instantaneo. Para PDFs escaneados (sem texto), cai no
openai_client (Vision). Expoe a mesma interface que o matcher ja usa.
"""

from __future__ import annotations

import logging

from . import openai_client as ai
from . import texto_utils

log = logging.getLogger(__name__)


def _extrair(pdf_bytes: bytes, tipo: str) -> "texto_utils.DadosTitulo":
    texto = texto_utils.texto_pdf(pdf_bytes)
    if texto and texto.strip():
        dados = texto_utils.parse(texto, tipo)
        if dados.chave or dados.valor:
            log.info("Extraido via texto (%s): chave=%s valor=%s",
                     tipo, bool(dados.chave), dados.valor)
            return dados
    log.info("Sem texto util (%s); usando OpenAI Vision", tipo)
    if tipo == "boleto":
        return ai.extrair_dados_boleto(pdf_bytes)
    return ai.extrair_dados_comprovante(pdf_bytes)


def extrair_dados_boleto(pdf_bytes: bytes):
    return _extrair(pdf_bytes, "boleto")


def extrair_dados_comprovante(pdf_bytes: bytes):
    return _extrair(pdf_bytes, "comprovante")

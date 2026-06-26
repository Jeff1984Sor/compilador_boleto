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
    dados = texto_utils.parse(texto, tipo) if (texto and texto.strip()) else None

    if dados:
        # Com linha digitavel no texto: confia (preciso e gratis).
        if dados.chave:
            log.info("Extraido via texto (%s): chave=ok valor=%s", tipo, dados.valor)
            return dados
        # Comprovante sem barras (ex.: PIX) casa por valor -> texto basta.
        if tipo == "comprovante" and dados.valor:
            log.info("Extraido via texto (comprovante sem barras): valor=%s", dados.valor)
            return dados

    # Boleto SEM linha digitavel no texto: ela pode estar como imagem/codigo de
    # barras (ex.: boleto Bradesco RECIBO). Cai no Vision para nao perder a chave.
    log.info("Sem linha digitavel no texto (%s); usando OpenAI Vision", tipo)
    ai_dados = (ai.extrair_dados_boleto if tipo == "boleto"
                else ai.extrair_dados_comprovante)(pdf_bytes)
    # Aproveita o valor lido do texto se o Vision nao trouxe.
    if ai_dados is not None and not ai_dados.valor and dados and dados.valor:
        ai_dados.valor = dados.valor
    return ai_dados if ai_dados is not None else dados


def extrair_dados_boleto(pdf_bytes: bytes):
    return _extrair(pdf_bytes, "boleto")


def extrair_dados_comprovante(pdf_bytes: bytes):
    return _extrair(pdf_bytes, "comprovante")

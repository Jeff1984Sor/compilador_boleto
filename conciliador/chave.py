"""Chave canonica de casamento: converte qualquer representacao de um titulo
para o CODIGO DE BARRAS de 44 digitos.

Motivo: o mesmo boleto pode aparecer como linha digitavel (47 dig, bancario;
48 dig, arrecadacao/DARE) no boleto e como codigo de barras (44 dig) no
comprovante - ou vice-versa. Comparar as strings cruas falha. Normalizando
tudo para o codigo de barras de 44 digitos, o casamento funciona sempre,
independente de qual lado mostrou qual formato.
"""

from __future__ import annotations

import re
from typing import Optional

_SO_DIGITOS = re.compile(r"\d")


def _digitos(texto: str) -> str:
    return "".join(_SO_DIGITOS.findall(texto or ""))


def _febraban_para_barras(d: str) -> str:
    """Linha digitavel bancaria (47 dig) -> codigo de barras (44 dig).

    Layout da linha digitavel (indices 0-based):
      campo1: [0:9] + DV[9]
      campo2: [10:20] + DV[20]
      campo3: [21:31] + DV[31]
      DV geral: [32]
      campo5: [33:47]  (fator de vencimento 4 + valor 10)
    Codigo de barras (44):
      banco/moeda(4) + DV_geral(1) + campo5(14) + campo_livre(25)
    """
    campo1 = d[0:9]
    campo2 = d[10:20]
    campo3 = d[21:31]
    dv_geral = d[32:33]
    campo5 = d[33:47]
    banco_moeda = campo1[0:4]
    campo_livre = campo1[4:9] + campo2 + campo3
    return banco_moeda + dv_geral + campo5 + campo_livre


def _arrecadacao_para_barras(d: str) -> str:
    """Linha digitavel de arrecadacao/DARE (48 dig) -> codigo de barras (44 dig).

    Sao 4 blocos de 12 digitos (11 uteis + 1 DV). Remove os 4 DVs (indices
    11, 23, 35, 47) para obter o codigo de barras de 44 digitos.
    """
    return d[0:11] + d[12:23] + d[24:35] + d[36:47]


def canonica(linha_digitavel: Optional[str]) -> Optional[str]:
    """Devolve o codigo de barras canonico (44 dig) ou None se nao reconhecer."""
    if not linha_digitavel:
        return None
    d = _digitos(linha_digitavel)
    if len(d) == 44:
        return d
    if len(d) == 47:
        return _febraban_para_barras(d)
    if len(d) == 48:
        return _arrecadacao_para_barras(d)
    # Tamanho inesperado: usa os digitos crus como chave (melhor que nada),
    # desde que tenham comprimento razoavel para nao casar lixo.
    return d if len(d) >= 20 else None

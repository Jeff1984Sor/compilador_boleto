"""Logica de conciliacao: casa boletos com comprovantes e gera arquivos resultantes."""

from __future__ import annotations

import io
import logging
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from . import gemini_client, pdf_utils

log = logging.getLogger(__name__)


@dataclass
class ResultadoBoleto:
    nome_arquivo: str           # nome final no ZIP (igual ao nome do boleto enviado)
    boleto_origem: str          # nome original do upload
    casado: bool
    chave: Optional[str] = None
    linha_digitavel: Optional[str] = None
    valor_boleto: Optional[str] = None
    vencimento: Optional[str] = None
    comprovante_pagina: Optional[int] = None  # 1-indexed, se casou
    pdf_relativo: Optional[str] = None        # caminho relativo no diretorio da sessao
    erro: Optional[str] = None


@dataclass
class ResultadoConciliacao:
    boletos: list[ResultadoBoleto] = field(default_factory=list)
    comprovantes_orfaos: list[int] = field(default_factory=list)  # paginas sem match
    total_comprovantes: int = 0

    @property
    def casados(self) -> int:
        return sum(1 for b in self.boletos if b.casado)

    @property
    def total_boletos(self) -> int:
        return len(self.boletos)

    def to_dict(self) -> dict:
        return {
            "boletos": [asdict(b) for b in self.boletos],
            "comprovantes_orfaos": self.comprovantes_orfaos,
            "total_comprovantes": self.total_comprovantes,
            "total_boletos": self.total_boletos,
            "casados": self.casados,
        }


def _processar_paralelo(items: list, func, max_workers: int = 6):
    """Executa func em paralelo sobre items (preservando ordem dos resultados)."""
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(func, items))


def conciliar(
    comprovantes_pdf: bytes,
    boletos: list[tuple[str, bytes]],
    sessao_dir: Path,
) -> ResultadoConciliacao:
    """
    Processa comprovantes + boletos e grava os PDFs resultantes em `sessao_dir`.

    Args:
        comprovantes_pdf: PDF unico contendo todos os comprovantes (1 por pagina).
        boletos: lista de (nome_arquivo, bytes) — 1 boleto por arquivo.
        sessao_dir: diretorio onde os PDFs resultantes serao gravados.

    Returns:
        ResultadoConciliacao com status de cada boleto.
    """
    sessao_dir.mkdir(parents=True, exist_ok=True)
    pasta_sem = sessao_dir / "sem_comprovante"

    log.info("Iniciando conciliacao: %d boletos", len(boletos))

    # 1. Divide comprovantes por pagina
    paginas_comprovantes = pdf_utils.split_por_pagina(comprovantes_pdf)
    log.info("Comprovantes: %d paginas detectadas", len(paginas_comprovantes))

    # 2. Extrai linha digitavel de cada comprovante (em paralelo)
    dados_comprovantes = _processar_paralelo(
        paginas_comprovantes,
        gemini_client.extrair_dados_comprovante,
    )

    # Indice: chave -> (indice_pagina, pdf_bytes)
    indice_comprovantes: dict[str, tuple[int, bytes]] = {}
    paginas_sem_chave: list[int] = []
    for idx, dados in enumerate(dados_comprovantes):
        chave = dados.chave
        if chave:
            # se a mesma chave aparecer 2x, prevalece a primeira ocorrencia
            indice_comprovantes.setdefault(chave, (idx, paginas_comprovantes[idx]))
        else:
            paginas_sem_chave.append(idx + 1)
            log.warning("Comprovante pagina %d: nao foi possivel extrair linha digitavel", idx + 1)

    # 3. Extrai linha digitavel de cada boleto (em paralelo)
    dados_boletos = _processar_paralelo(
        [b[1] for b in boletos],
        gemini_client.extrair_dados_boleto,
    )

    # 4. Casa e gera PDF resultante
    resultado = ResultadoConciliacao(total_comprovantes=len(paginas_comprovantes))
    usados: set[str] = set()

    for (nome, boleto_bytes), dados in zip(boletos, dados_boletos):
        nome_final = _sanitizar_nome_pdf(nome)
        chave = dados.chave
        r = ResultadoBoleto(
            nome_arquivo=nome_final,
            boleto_origem=nome,
            casado=False,
            chave=chave,
            linha_digitavel=dados.linha_digitavel,
            valor_boleto=dados.valor,
            vencimento=dados.vencimento,
        )

        if chave and chave in indice_comprovantes and chave not in usados:
            idx_pag, comp_bytes = indice_comprovantes[chave]
            usados.add(chave)
            pdf_merged = pdf_utils.merge_pdfs([boleto_bytes, comp_bytes])
            dest = sessao_dir / nome_final
            dest.write_bytes(pdf_merged)
            r.casado = True
            r.comprovante_pagina = idx_pag + 1
            r.pdf_relativo = nome_final
        else:
            # Sem comprovante: copia o boleto sozinho para pasta separada
            pasta_sem.mkdir(parents=True, exist_ok=True)
            dest = pasta_sem / nome_final
            dest.write_bytes(boleto_bytes)
            r.pdf_relativo = f"sem_comprovante/{nome_final}"
            if not chave:
                r.erro = "Nao foi possivel ler a linha digitavel do boleto"
            else:
                r.erro = "Nenhum comprovante com essa linha digitavel"

        resultado.boletos.append(r)

    # Comprovantes que ninguem usou (paginas)
    orfaos = []
    for chave, (idx, _) in indice_comprovantes.items():
        if chave not in usados:
            orfaos.append(idx + 1)
    resultado.comprovantes_orfaos = sorted(orfaos + paginas_sem_chave)

    log.info("Conciliacao concluida: %d/%d casados", resultado.casados, resultado.total_boletos)
    return resultado


def montar_zip(sessao_dir: Path, resultado: ResultadoConciliacao) -> bytes:
    """Empacota tudo da sessao em um ZIP em memoria."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for boleto in resultado.boletos:
            if not boleto.pdf_relativo:
                continue
            caminho = sessao_dir / boleto.pdf_relativo
            if caminho.exists():
                zf.write(caminho, arcname=boleto.pdf_relativo)
        # Relatorio
        zf.writestr("_conciliacao.txt", _gerar_relatorio(resultado))
    return buf.getvalue()


def _gerar_relatorio(r: ResultadoConciliacao) -> str:
    linhas = [
        "=== Relatorio de Conciliacao ===",
        f"Total de boletos: {r.total_boletos}",
        f"Total de comprovantes (paginas): {r.total_comprovantes}",
        f"Boletos casados: {r.casados}",
        f"Boletos sem comprovante: {r.total_boletos - r.casados}",
        f"Comprovantes orfaos (paginas): {r.comprovantes_orfaos or 'nenhum'}",
        "",
        "--- Detalhe por boleto ---",
    ]
    for b in r.boletos:
        status = "OK" if b.casado else "SEM COMPROVANTE"
        extra = f" (pag.{b.comprovante_pagina})" if b.comprovante_pagina else ""
        erro = f" — {b.erro}" if b.erro else ""
        linhas.append(f"[{status}] {b.nome_arquivo}{extra}{erro}")
    return "\n".join(linhas) + "\n"


def _sanitizar_nome_pdf(nome: str) -> str:
    """Garante que o nome termina com .pdf e remove path traversal."""
    base = os.path.basename(nome).strip()
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    # remove caracteres perigosos para sistema de arquivos
    base = base.replace("\\", "_").replace("/", "_")
    return base

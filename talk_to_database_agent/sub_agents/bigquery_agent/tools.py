import asyncio
import base64
import datetime
import decimal
import logging
import math
import re
from typing import Any

from google.adk.agents import Context
from google.cloud import bigquery

from talk_to_database_agent.app_utils.config import settings
from talk_to_database_agent.sub_agents.bigquery_agent.bq import get_bq_client

logger = logging.getLogger(__name__)

# O LLM costuma devolver a query dentro de um bloco markdown.
_MARKDOWN_FENCE_RE = re.compile(
    r"```[ \t]*(?:sql|bigquery|googlesql)?[ \t]*\r?\n?(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def _strip_markdown_fence(sql: str) -> str:
    """Extrai a query de dentro de um bloco markdown, se houver."""
    match = _MARKDOWN_FENCE_RE.search(sql)
    if match:
        return match.group(1)

    # Bloco aberto e nunca fechado: remove só a cerca inicial.
    stripped = sql.strip()
    if stripped.startswith("```"):
        return re.sub(
            r"^```[ \t]*(?:sql|bigquery|googlesql)?[ \t]*\r?\n?",
            "",
            stripped,
            flags=re.IGNORECASE,
        )
    return sql


def _mask_literals_and_comments(sql: str) -> str:
    """Troca strings, identificadores citados e comentários por espaços.

    Permite procurar por `;` e palavras-chave sem cair em falso positivo
    quando elas aparecem dentro de um literal (ex.: WHERE nome = 'DROP').
    """
    masked: list[str] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if sql.startswith("--", index) or char == "#":
            end = sql.find("\n", index)
            index = length if end == -1 else end
            masked.append(" ")
            continue

        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
            masked.append(" ")
            continue

        if char in "'\"`":
            quote = char
            if quote in "'\"" and sql.startswith(quote * 3, index):
                end = sql.find(quote * 3, index + 3)
                index = length if end == -1 else end + 3
            else:
                index += 1
                while index < length:
                    if sql[index] == "\\":
                        index += 2
                        continue
                    if sql[index] == quote:
                        index += 1
                        break
                    index += 1
            masked.append(" ")
            continue

        masked.append(char)
        index += 1

    return "".join(masked)


def sanitize_sql(sql: str) -> str:
    """Normaliza a query SQL gerada pelo LLM antes de validar e executar.

    Aplica as limpezas que o modelo costuma exigir:
    - remove o bloco markdown (```sql ... ```) em volta da query;
    - remove espaços e quebras de linha nas bordas;
    - remove o ponto e vírgula final.

    Args:
        sql: A query crua, como veio do modelo.

    Returns:
        A query limpa, pronta para ser validada por check_sql_read_only.

    Raises:
        ValueError: se a query estiver vazia ou contiver mais de um statement.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("A query SQL está vazia.")

    cleaned = _strip_markdown_fence(sql).strip()
    # Ponto e vírgula final é inofensivo, mas remover simplifica a checagem
    # de múltiplos statements abaixo.
    cleaned = re.sub(r"[;\s]+$", "", cleaned)

    if not cleaned:
        raise ValueError("A query SQL está vazia.")

    if ";" in _mask_literals_and_comments(cleaned):
        raise ValueError(
            "Apenas um statement por execução é permitido. "
            "Remova os `;` intermediários da query."
        )

    return cleaned


def sanitize_value(value: Any) -> Any:
    """Converte um valor do BigQuery em algo serializável em JSON."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Infinity não são JSON válido.
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize_value(item) for item in value]
    return str(value)


def sanitize_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """Converte as linhas do BigQuery em tipos serializáveis em JSON."""
    return [[sanitize_value(value) for value in row] for row in rows]


# Só estes dois abrem uma query de leitura em GoogleSQL. Como `sanitize_sql` já
# rejeitou múltiplos statements, checar a primeira palavra é suficiente para
# barrar DDL/DML — inclusive `EXPORT DATA ... AS SELECT`, que uma denylist de
# palavras-chave deixava passar.
_READ_ONLY_STATEMENTS = frozenset({"SELECT", "WITH"})

# Defesa em profundidade: nenhuma destas pode aparecer como palavra inteira no
# corpo da query. `\b` é o que evita o falso positivo — `updated_at` não casa
# com `\bUPDATE\b`, `created_date` não casa com `\bCREATE\b`.
_FORBIDDEN_KEYWORD_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE"
    r"|GRANT|REVOKE|EXPORT|LOAD|CALL|DECLARE|EXECUTE)\b",
    re.IGNORECASE,
)


def check_sql_read_only(sql: str) -> bool:
    """Verifica se a query SQL é somente leitura (SELECT).

    A checagem roda sobre a query mascarada por `_mask_literals_and_comments`,
    não sobre o texto cru: senão `WHERE city = 'Update Springs'` ou um
    comentário `-- create a summary` derrubavam um SELECT perfeitamente válido.

    Args:
        sql: A query já normalizada por sanitize_sql.

    Returns:
        True se a query for somente leitura.
    """
    masked = _mask_literals_and_comments(sql).strip()

    # `(SELECT ...) UNION ALL (SELECT ...)` é leitura e começa com parêntese.
    masked = masked.lstrip("( \t\r\n")

    first_word = re.match(r"[A-Za-z_]+", masked)
    if not first_word or first_word.group(0).upper() not in _READ_ONLY_STATEMENTS:
        return False

    return not _FORBIDDEN_KEYWORD_RE.search(masked)


def format_bytes(num_bytes: int) -> str:
    """Formata uma contagem de bytes para caber numa mensagem de erro."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.2f} TB"


def _estimate_bytes(bq: bigquery.Client, sql: str) -> int:
    """Quantos bytes a query varreria, via dry run (não é cobrado)."""
    job = bq.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    return job.total_bytes_processed or 0


def _run_sql_query_blocking(sql: str) -> dict:
    """Faz o trabalho bloqueante no BigQuery. Roda numa thread — ver run_sql_query."""
    bq = get_bq_client()
    max_bytes = settings.bq_max_bytes_billed

    # O dry run é gratuito e permite devolver um erro que o modelo consegue
    # corrigir ("reduza o período", "selecione menos colunas"), em vez do 400
    # opaco que `maximum_bytes_billed` produz depois que a query já falhou.
    estimated_bytes = _estimate_bytes(bq, sql)
    if estimated_bytes > max_bytes:
        logger.warning(
            "Query rejeitada por custo: %s > limite de %s",
            format_bytes(estimated_bytes),
            format_bytes(max_bytes),
        )
        return {
            "status": "error",
            "error": (
                f"Esta query varreria {format_bytes(estimated_bytes)}, acima do "
                f"limite de {format_bytes(max_bytes)}. Lembre que LIMIT não "
                "reduz os bytes lidos: restrinja as colunas do SELECT (evite "
                "`SELECT *`) e filtre a coluna de data/partição no WHERE."
            ),
            "sql": sql,
            "estimated_bytes": estimated_bytes,
            "max_bytes_billed": max_bytes,
        }

    logger.info(
        "Executing BigQuery SQL (est. %s)… %s", format_bytes(estimated_bytes), sql
    )
    # `maximum_bytes_billed` é a rede de segurança: a estimativa do dry run pode
    # ficar defasada se a tabela crescer entre as duas chamadas.
    query_job = bq.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=max_bytes,
            job_timeout_ms=int(settings.bq_query_timeout_seconds * 1000),
        ),
    )
    result = query_job.result()

    columns = [field.name for field in result.schema]
    rows = sanitize_rows([list(row.values()) for row in result])

    logger.info(
        "Query returned %d rows, billed %s",
        len(rows),
        format_bytes(query_job.total_bytes_billed or 0),
    )

    return {
        "status": "success",
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "bytes_billed": query_job.total_bytes_billed or 0,
    }


async def run_sql_query(
    sql: str,
    tool_context: Context
) -> dict:
    """Executa uma query SQL somente leitura no warehouse BigQuery.

    Use esta ferramenta DEPOIS de identificar as tabelas e colunas corretas
    no schema fornecido. Apenas queries SELECT são permitidas — INSERT,
    UPDATE, DELETE, DDL e qualquer operação de escrita serão rejeitadas.

    Há um limite de bytes varridos por query. LIMIT não reduz os bytes lidos:
    para ficar dentro do limite, selecione apenas as colunas necessárias e
    filtre a coluna de data no WHERE.

    Args:
        sql: A query SQL SELECT a executar. DEVE usar nomes de tabelas
             completos (`projeto.dataset.tabela`).

    Returns:
        dict com nomes de colunas e linhas, ou uma mensagem de erro.
    """
    logger.info("run_sql_query: sql= %s ", sql)

    try:
        sql = sanitize_sql(sql)
    except ValueError as exc:
        logger.error("SQL sanitization failed: %s", exc)
        return {
            "status": "error",
            "error": str(exc),
            "sql": sql,
        }

    if not check_sql_read_only(sql):
        logger.error("SQL query is not read-only: %s", sql)
        return {
            "status": "error",
            "error": "Apenas queries SELECT são permitidas. Operações de escrita são proibidas.",
        }

    try:
        # O cliente do BigQuery é síncrono e uma query leva segundos. Chamada
        # direta, ela travaria o event loop inteiro do uvicorn — nenhuma outra
        # requisição avança enquanto ela roda.
        return await asyncio.to_thread(_run_sql_query_blocking, sql)
    except Exception as exc:
        logger.exception("BigQuery execution failed")
        return {
            "status": "error",
            "error": f"BigQuery execution failed: {exc}",
            "sql": sql,
        }
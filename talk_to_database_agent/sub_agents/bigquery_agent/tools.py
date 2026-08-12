import base64
import datetime
import decimal
import logging
import math
import re
from typing import Any

from google.adk.agents import Context
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


def check_sql_read_only(sql: str) -> bool:
    """Verifica se a query SQL é somente leitura (SELECT)."""
    forbidden_keywords = ["INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"]
    sql_upper = sql.upper()
    return not any(keyword in sql_upper for keyword in forbidden_keywords)

def run_sql_query(
    sql: str,
    tool_context: Context
) -> dict:
    """Executa uma query SQL somente leitura no warehouse BigQuery.

    Use esta ferramenta DEPOIS de identificar as tabelas e colunas corretas
    via search_database_context. Apenas queries SELECT são permitidas —
    INSERT, UPDATE, DELETE, DDL e qualquer operação de escrita serão
    rejeitadas.

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
        bq = get_bq_client()
        logger.info("Executing BigQuery SQL… %s", sql)
        query_job = bq.query(sql)
        result = query_job.result()

        columns = [field.name for field in result.schema]
        rows = [list(row.values()) for row in result]
        rows = sanitize_rows(rows)
    except Exception as exc:
        logger.exception("BigQuery execution failed")
        return {
            "status": "error",
            "error": f"BigQuery execution failed: {exc}",
            "sql": sql,
        }

    logger.info("Query returned %d rows", len(rows))

    response: dict = {
        "status": "success",
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }


    return response
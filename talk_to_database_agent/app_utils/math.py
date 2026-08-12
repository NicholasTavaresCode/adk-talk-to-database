import math, logging

logger = logging.getLogger(__name__)

def calculate(expression: str) -> dict:
    """Avalia uma expressão matemática e retorna o resultado exato.

    Use esta ferramenta sempre que precisar calcular qualquer aritmética:
    somas, diferenças, produtos, divisões, percentuais, razões, etc.
    A expressão é avaliada com segurança — apenas operações matemáticas
    são permitidas.

    Args:
        expression: Expressão matemática a avaliar.
            Exemplos: "1234567.89 + 9876543.21", "150000 / 3500000 * 100",
            "(850000 - 720000) / 720000 * 100", "sum([10, 20, 30, 40])".

    Returns:
        dict com o resultado avaliado ou uma mensagem de erro.
    """
    logger.info("calculate: expression=%r", expression)

    safe_names = {
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "sum": sum,
        "len": len,
        "pow": pow,
        # math module functions
        "sqrt": math.sqrt,
        "ceil": math.ceil,
        "floor": math.floor,
        "log": math.log,
        "log10": math.log10,
        "pi": math.pi,
        "e": math.e,
    }

    try:
        import re

        cleaned = expression
        for name in safe_names:
            cleaned = cleaned.replace(name, "")

        if re.search(r"[a-zA-Z_]", cleaned):
            return {
                "status": "error",
                "error": (
                    "A expressão contém nomes não permitidos. "
                    "Apenas operações matemáticas e estas funções são permitidas: "
                    + ", ".join(sorted(safe_names.keys()))
                ),
            }

        result = eval(expression, {"__builtins__": {}}, safe_names)  # noqa: S307
        logger.info("calculate: result=%s", result)
        return {"status": "success", "result": result}
    except ZeroDivisionError:
        return {"status": "error", "error": "Divisão por zero."}
    except Exception as exc:
        logger.exception("calculate failed for: %r", expression)
        return {"status": "error", "error": f"Cálculo falhou: {exc}"}


def percentage_change(old_value: float, new_value: float) -> dict:
    """Calcula a variação percentual entre dois valores.

    Use para comparações entre períodos: safra a safra,
    ano a ano, mês a mês, etc.

    Args:
        old_value: O valor de referência (base).
        new_value: O novo valor (comparação).

    Returns:
        dict com percentage_change, absolute_difference e direction.
    """
    logger.info("percentage_change: old=%s, new=%s", old_value, new_value)

    if old_value == 0:
        return {
            "status": "error",
            "error": "Não é possível calcular variação percentual a partir de um valor base zero.",
        }

    diff = new_value - old_value
    pct = (diff / old_value) * 100

    return {
        "status": "success",
        "old_value": old_value,
        "new_value": new_value,
        "absolute_difference": round(diff, 2),
        "percentage_change": round(pct, 2),
        "direction": "aumento" if diff > 0 else "redução" if diff < 0 else "sem alteração",
    }


def proportion(part: float, total: float) -> dict:
    """Calcula qual percentual uma parte representa do total.

    Use para análise de participação/composição: qual % do custo total
    pertence a um departamento, categoria, etc.

    Args:
        part: O valor da parcela.
        total: O valor total.

    Returns:
        dict com o percentual de participação.
    """
    logger.info("proportion: part=%s, total=%s", part, total)

    if total == 0:
        return {"status": "error", "error": "O total não pode ser zero."}

    pct = (part / total) * 100
    return {
        "status": "success",
        "part": part,
        "total": total,
        "percentage": round(pct, 2),
    }


MATH_TOOLS = [calculate, percentage_change, proportion]

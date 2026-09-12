import datetime
from zoneinfo import ZoneInfo

MONTH_NAMES = {
    1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril",
    5: "Maio", 6: "Junho", 7: "Julho", 8: "Agosto",
    9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}

def build_timezone_metadata():
    """Compute temporal metadata at call time in America/Sao_Paulo."""
    now = datetime.datetime.now(ZoneInfo("America/Sao_Paulo"))
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    now_date = now.strftime("%d/%m/%Y")
    month_year = f"{MONTH_NAMES[now.month]}/{now.year}"
    fy_current = now.year if now.month >= 4 else now.year - 1
    fy_prev = fy_current - 1

    return {
        "now_str": now_str,
        "now_date": now_date,
        "month_year": month_year,
        "fy_current": fy_current,
        "fy_prev": fy_prev
    }

import datetime
from zoneinfo import ZoneInfo

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

def build_timezone_metadata():
    """Compute temporal metadata at call time."""
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
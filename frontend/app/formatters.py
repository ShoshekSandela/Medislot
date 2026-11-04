from datetime import datetime, time, date
from decimal import Decimal, InvalidOperation

def time_input(value):
    if value in (None, ""): return ""
    if isinstance(value, time): return value.strftime("%H:%M")
    parts = str(value).strip().split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}" if len(parts) >= 2 else str(value)

def time_display(value):
    v=time_input(value)
    if not v: return "—"
    h,m=map(int,v.split(":")); return f"{(h%12) or 12}:{m:02d} {'AM' if h<12 else 'PM'}"

def parse_datetime(value):
    if not value: return None
    if isinstance(value, datetime): return value
    s=str(value).replace("T", " ")
    try: return datetime.fromisoformat(s)
    except ValueError: return None

def date_display(value):
    d=parse_datetime(value)
    if d: return d.strftime("%d %b %Y")
    try: return date.fromisoformat(str(value)).strftime("%d %b %Y")
    except (ValueError,TypeError): return str(value or "—")

def money(value):
    try: return "₹" + format(Decimal(str(value or 0)), ",.2f")
    except (InvalidOperation,ValueError): return "₹0.00"

def as_bool(value): return str(value).lower() in ("1","true","yes","on")

def full_day_range(start_date, end_date):
    return f"{start_date} 00:00:00", f"{end_date} 23:59:59"

def normalise_time_seconds(value):
    v=time_input(value)
    return f"{v}:00" if v else ""

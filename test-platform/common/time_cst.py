# -*- coding: utf-8 -*-
"""东八区时间统一入口 —— 筛选窗口、publishTime、对账日期。

用法::

    from time_cst import now_cst, now_str, today_str, window_around, future_empty_window, parse_dt

禁止各模块再各自 ``strftime`` / ``timedelta`` 拼一套东八区字符串。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

FMT_DT = "%Y-%m-%d %H:%M:%S"
FMT_DATE = "%Y-%m-%d"


def now_cst() -> datetime:
    """当前东八区 aware datetime。"""
    return datetime.now(CST)


def now_str(fmt=FMT_DT) -> str:
    """当前东八区时间字符串（默认 ``YYYY-MM-DD HH:MM:SS``）。"""
    return now_cst().strftime(fmt)


def today_str() -> str:
    return now_cst().strftime(FMT_DATE)


def today() -> date:
    return now_cst().date()


def parse_dt(s):
    """解析常见时间字符串 / 秒或毫秒时间戳。失败返回 None。"""
    if s is None or s == "":
        return None
    raw = str(s).strip()
    if raw.isdigit() and len(raw) in (10, 13):
        try:
            ts = int(raw)
            return datetime.fromtimestamp(ts / 1000 if len(raw) == 13 else ts, tz=CST)
        except (ValueError, OSError, OverflowError):
            return None
    raw = raw.replace("T", " ")[:19]
    for fmt in (FMT_DT, FMT_DATE):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def format_dt(dt, fmt=FMT_DT) -> str:
    if dt is None:
        return ""
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return dt.strftime(FMT_DATE if fmt == FMT_DATE else FMT_DATE)
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=CST)
    else:
        dt = dt.astimezone(CST)
    return dt.strftime(fmt)


def window_around(sample_time, *, before_hours=24, after_hours=24, fmt=FMT_DT):
    """以样本时间为中心推闭区间，返回 (from_str, to_str)。

    sample_time 可为 datetime / 字符串；解析失败返回 (None, None)。
    """
    t = sample_time if isinstance(sample_time, datetime) else parse_dt(sample_time)
    if t is None:
        return None, None
    if t.tzinfo is None:
        t = t.replace(tzinfo=CST)
    return format_dt(t - timedelta(hours=before_hours), fmt), format_dt(
        t + timedelta(hours=after_hours), fmt
    )


def window_days(days=7, *, end=None, fmt=FMT_DATE):
    """近 N 天闭区间（含今天），返回 (start_str, end_str)。"""
    end_d = end
    if end_d is None:
        end_d = now_cst()
    elif isinstance(end_d, date) and not isinstance(end_d, datetime):
        end_d = datetime(end_d.year, end_d.month, end_d.day, tzinfo=CST)
    elif isinstance(end_d, datetime) and end_d.tzinfo is None:
        end_d = end_d.replace(tzinfo=CST)
    start_d = end_d - timedelta(days=max(0, int(days) - 1))
    return format_dt(start_d, fmt), format_dt(end_d, fmt)


def future_empty_window(*, days_ahead=30, span_days=1, fmt=FMT_DT):
    """未来空窗：用于 F 系列 require_empty。返回 (from_str, to_str)。"""
    start = now_cst() + timedelta(days=days_ahead)
    end = start + timedelta(days=span_days)
    return format_dt(start, fmt), format_dt(end, fmt)


def date_00(stat_date) -> str:
    """统计日 00:00:00。"""
    raw = str(stat_date).strip()[:10]
    return f"{raw} 00:00:00"


def date_59(stat_date) -> str:
    """统计日 23:59:59。"""
    raw = str(stat_date).strip()[:10]
    return f"{raw} 23:59:59"

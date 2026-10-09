# -*- coding: utf-8 -*-
"""只读查询工具统一分页（默认每页 5 条）。"""

DEFAULT_PAGE_SIZE = 5
MAX_PAGE_SIZE = 50


def parse_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_page(v, default=1):
    page = parse_int(v)
    return max(1, page if page is not None else default)


def parse_page_size(v, default=DEFAULT_PAGE_SIZE, max_size=MAX_PAGE_SIZE):
    size = parse_int(v)
    if size is None:
        size = default
    return max(1, min(max_size, size))


def page_meta(total, page, size):
    total = int(total or 0)
    pages = max(1, (total + size - 1) // size) if total else 1
    return {"total": total, "current": page, "size": size, "pages": pages}


def fetch_page(cur, count_sql, count_params, list_sql, list_params, page=1, size=DEFAULT_PAGE_SIZE):
    """COUNT + LIMIT/OFFSET，返回 {records, total, current, size, pages}。"""
    cur.execute(count_sql, count_params)
    row = cur.fetchone()
    total = int((row.get("n") if isinstance(row, dict) else row[0]) or 0)
    page = parse_page(page)
    size = parse_page_size(size)
    offset = (page - 1) * size
    cur.execute(list_sql + " LIMIT %s OFFSET %s", list_params + [size, offset])
    records = list(cur.fetchall())
    return {"records": records, **page_meta(total, page, size)}

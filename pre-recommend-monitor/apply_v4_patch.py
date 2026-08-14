#!/usr/bin/env python3
"""One-off deterministic patch from monitor schema v3 to v4."""
from pathlib import Path

path = Path(__file__).with_name("monitor_v3.py")
text = path.read_text(encoding="utf-8")
if '"schema_version": 4' in text:
    print("monitor_v3.py is already schema v4")
    raise SystemExit(0)


def replace(old: str, new: str, label: str) -> None:
    global text
    if old not in text:
        raise SystemExit(f"patch anchor not found: {label}")
    text = text.replace(old, new)


replace(
    'FULL_DATE = re.compile(r"(?P<y>20\\d{2})[年./-]\\s*(?P<m>\\d{1,2})[月./-]\\s*(?P<d>\\d{1,2})(?:日|号)?")\n'
    'MONTH_DAY = re.compile(r"(?<!\\d)(?P<m>\\d{1,2})月\\s*(?P<d>\\d{1,2})(?:日|号)?")\n',
    'FULL_DATE = re.compile(r"(?P<y>20\\d{2})\\s*[年./-]\\s*(?P<m>\\d{1,2})\\s*[月./-]\\s*(?P<d>\\d{1,2})\\s*(?:日|号)?")\n'
    'MONTH_DAY = re.compile(r"(?<!\\d)(?P<m>\\d{1,2})\\s*月\\s*(?P<d>\\d{1,2})\\s*(?:日|号)?")\n',
    "Chinese date spacing",
)
replace(
    'NUMERIC_MD = re.compile(r"(?<!\\d)(?P<m>\\d{1,2})[./-](?P<d>\\d{1,2})(?!\\d)")\n'
    'ANY_DATE = re.compile(r"(?:20\\d{2}[年./-]\\s*)?\\d{1,2}(?:月|[./-])\\s*\\d{1,2}(?:日|号)?")\n',
    'ANY_DATE = re.compile(\n'
    '    r"(?:20\\d{2}\\s*[年./-]\\s*)?\\d{1,2}\\s*月\\s*\\d{1,2}\\s*(?:日|号)?"\n'
    '    r"|20\\d{2}\\s*[./-]\\s*\\d{1,2}\\s*[./-]\\s*\\d{1,2}"\n'
    ')\n'
    'EVENT_TERMS = ("夏令营", "校园开放日", "暑期学校", "招生宣传日", "选拔营")\n'
    'EVENT_ACTIONS = ("通知", "公告", "方案", "安排", "招生", "报名", "申请", "招募", "简章")\n'
    'NEWS_RESULTS = ("圆满举行", "成功举办", "顺利举行", "受邀参加", "召开", "纪实", "回顾", "风采", "闭营", "开营仪式")\n'
    'TITLE_SCOPE = ("计算机", "软件", "人工智能", "网络空间安全", "网络安全", "数据科学", "智能科学", "信息学院", "信息科学", "电子信息", "自动化", "模式识别", "智能医学")\n'
    'GENERAL_NOTICE = ("接收推荐免试研究生", "推荐免试研究生预报名", "推荐免试研究生工作办法", "推荐免试研究生招生章程", "推免生预报名", "推荐免试研究生预报名的通知")\n',
    "event and scope constants",
)
replace(
    'def in_scope(text: str, cfg: dict[str, Any]) -> bool:\n'
    '    return any(k in text for k in cfg["major_keywords"]) or any(k in text for k in GENERAL_SCOPE)\n',
    'def headline_notice(text: str, cfg: dict[str, Any]) -> bool:\n'
    '    text = clean(text)\n'
    '    if not has_notice(text, cfg) or any(word in text for word in NEWS_RESULTS):\n'
    '        return False\n'
    '    if any(word in text for word in EVENT_TERMS) and not any(word in text for word in EVENT_ACTIONS):\n'
    '        return False\n'
    '    return True\n\n\n'
    'def scope_ok(school: str, title: str, candidate_title: str, body: str) -> bool:\n'
    '    headline = clean(f"{candidate_title} {title}")\n'
    '    lead = clean(body[:1400])\n'
    '    if any(word in headline for word in TITLE_SCOPE):\n'
    '        return True\n'
    '    if school in headline and any(word in headline for word in GENERAL_NOTICE):\n'
    '        return True\n'
    '    return any(word in lead for word in TITLE_SCOPE) and any(\n'
    '        word in headline for word in GENERAL_NOTICE + EVENT_TERMS\n'
    '    )\n\n\n'
    'def normalize_title(value: str) -> str:\n'
    '    value = clean(value)\n'
    '    if " | " in value:\n'
    '        first = value.split(" | ", 1)[0].strip()\n'
    '        if headline_fragment(first):\n'
    '            value = first\n'
    '    parts = value.split()\n'
    '    if len(parts) >= 2 and len(parts) % 2 == 0:\n'
    '        half = len(parts) // 2\n'
    '        if parts[:half] == parts[half:]:\n'
    '            value = " ".join(parts[:half])\n'
    '    for _ in range(2):\n'
    '        half = len(value) // 2\n'
    '        if len(value) > 20 and value[:half].strip() == value[half:].strip():\n'
    '            value = value[:half].strip()\n'
    '    return value[:180]\n\n\n'
    'def headline_fragment(value: str) -> bool:\n'
    '    return any(word in value for word in ("推免", "推荐免试", "夏令营", "开放日", "暑期学校", "直博", "招生宣传日"))\n\n\n'
    'def title_signature(value: str) -> str:\n'
    '    value = normalize_title(value)\n'
    '    return re.sub(r"[^0-9A-Za-z\\u4e00-\\u9fff]+", "", value)\n',
    "headline and scope filters",
)
replace(
    '            value = clean(node.get_text(" ", strip=True))\n'
    '            if 5 <= len(value) <= 220 and value not in values:\n'
    '                values.append(value)\n'
    '    for value in (clean(fallback), clean(soup.title.get_text(" ", strip=True))[:220] if soup.title else ""):\n',
    '            value = normalize_title(node.get_text(" ", strip=True))\n'
    '            if 5 <= len(value) <= 180 and value not in values:\n'
    '                values.append(value)\n'
    '    for value in (normalize_title(fallback), normalize_title(soup.title.get_text(" ", strip=True)) if soup.title else ""):\n',
    "title normalization",
)
replace(
    '        return keywords, int(bool(re.search(r"20\\d{2}", value))), len(value)\n',
    '        return keywords, int(bool(re.search(r"20\\d{2}", value))), -abs(len(value) - 70)\n',
    "title scoring",
)
text = text.replace('if has_notice(title, cfg) and year_in_text(', 'if headline_notice(title, cfg) and year_in_text(')
text = text.replace('if has_notice(candidate_text, cfg) and (year_in_text(', 'if headline_notice(candidate_text, cfg) and (year_in_text(')
replace('    for pattern in (MONTH_DAY, NUMERIC_MD):\n', '    for pattern in (MONTH_DAY,):\n', "numeric false positives")
replace(
    '    deadlines: list[date] = []\n'
    '    for value in times:\n'
    '        if any(k in value for k in DEADLINE_WORDS):\n'
    '            deadlines.extend(parse_dates(value, default_year))\n',
    '    deadlines: list[date] = []\n'
    '    for value in times:\n'
    '        explicit = any(k in value for k in DEADLINE_WORDS)\n'
    '        application_range = any(k in value for k in APPLICATION_WORDS) and any(\n'
    '            k in value for k in ("至", "到", "前", "之前")\n'
    '        )\n'
    '        if explicit or application_range:\n'
    '            deadlines.extend(parse_dates(value, default_year))\n',
    "range deadline inference",
)
replace(
    '    if not has_notice(blob, cfg) or not in_scope(blob, cfg) or not year_in_text(blob, year) or not title_year_ok(title or candidate.title, year):\n'
    '        return None\n',
    '    headline = clean(f"{candidate.title} {title}")\n'
    '    if (\n'
    '        not headline_notice(headline, cfg)\n'
    '        or not scope_ok(candidate.school, title, candidate.title, body)\n'
    '        or not year_in_text(blob, year)\n'
    '        or not title_year_ok(title or candidate.title, year)\n'
    '    ):\n'
    '        return None\n',
    "build filter",
)
replace(
    '    return Notice(candidate.school, candidate.priority, title or candidate.title, url, published, times, notice_status(times, now.date(), published), clean(body[:320]), fingerprint)\n',
    '    final_title = normalize_title(title or candidate.title)\n'
    '    return Notice(candidate.school, candidate.priority, final_title, url, published, times, notice_status(times, now.date(), published), clean(body[:320]), fingerprint)\n',
    "final title",
)
replace(
    '    dedup: dict[str, Notice] = {}\n'
    '    for item in notices:\n'
    '        key = notice_key(item)\n'
    '        if key not in dedup or len(" ".join(item.key_times)) > len(" ".join(dedup[key].key_times)):\n'
    '            dedup[key] = item\n'
    '    notices = sorted(dedup.values(), key=sort_key)\n',
    '    dedup: dict[str, Notice] = {}\n'
    '    for item in notices:\n'
    '        key = f"{item.school}|{title_signature(item.title)}"\n'
    '        if key not in dedup or len(" ".join(item.key_times)) > len(" ".join(dedup[key].key_times)):\n'
    '            dedup[key] = item\n'
    '    notices = sorted(dedup.values(), key=sort_key)\n',
    "title deduplication",
)
replace(
    '    old_items = old.get("items", {})\n    first_run = not bool(old_items)\n',
    '    old_items = old.get("items", {}) if old.get("schema_version") == 4 else {}\n    first_run = not bool(old_items)\n',
    "schema reset",
)
text = text.replace('{"schema_version": 3,', '{"schema_version": 4,')
path.write_text(text, encoding="utf-8")
print("patched monitor_v3.py to schema v4")

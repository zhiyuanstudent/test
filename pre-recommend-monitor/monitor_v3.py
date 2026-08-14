#!/usr/bin/env python3
"""Daily official-site crawler for Chinese CS pre-recommendation notices."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import sys
from collections import deque
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = ROOT / "schools.yml"
SEEDS = ROOT / "official_seeds.yml"
STATE = DATA / "state.json"
REPORT = DATA / "latest.md"
ALERTS = DATA / "alerts.md"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/126 Safari/537.36 PreRecommendMonitor/3.0"
)
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "from", "spm", "src"}
ASSETS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".mp4", ".mp3")
NAV_WORDS = (
    "研究生招生", "研究生教育", "招生信息", "招生动态", "招生工作", "招生通知", "招生公告",
    "硕士招生", "博士招生", "通知公告", "推免", "推荐免试", "夏令营", "校园开放日",
    "优秀大学生", "人才培养", "计算机学院", "软件学院", "人工智能学院", "网络空间安全学院", "信息学院",
)
NAV_PATHS = ("admission", "graduate", "yjs", "yz", "zhaosheng", "zsxx", "notice", "news", "tzgg", "list", "summer", "camp")
GENERAL_SCOPE = ("招生简章", "接收办法", "接收章程", "接收推荐免试", "推荐免试研究生招生", "各院系", "全校")
DEADLINE_WORDS = ("截止", "截至", "结束", "关闭", "逾期", "最后")
APPLICATION_WORDS = ("报名", "申请", "提交", "填报", "材料", "系统开放", "注册")
ASSESSMENT_WORDS = ("复试", "面试", "考核", "选拔", "宣讲", "入营", "报到", "确认")
FULL_DATE = re.compile(r"(?P<y>20\d{2})\s*[年./-]\s*(?P<m>\d{1,2})\s*[月./-]\s*(?P<d>\d{1,2})\s*(?:日|号)?")
MONTH_DAY = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*(?:日|号)?")
ANY_DATE = re.compile(
    r"(?:20\d{2}\s*[年./-]\s*)?\d{1,2}\s*月\s*\d{1,2}\s*(?:日|号)?"
    r"|20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}"
)
EVENT_TERMS = ("夏令营", "校园开放日", "暑期学校", "招生宣传日", "选拔营")
EVENT_ACTIONS = ("通知", "公告", "方案", "安排", "招生", "报名", "申请", "招募", "简章")
NEWS_RESULTS = ("圆满举行", "成功举办", "顺利举行", "受邀参加", "召开", "纪实", "回顾", "风采", "闭营", "开营仪式")
TITLE_SCOPE = ("计算机", "软件", "人工智能", "网络空间安全", "网络安全", "数据科学", "智能科学", "信息学院", "信息科学", "电子信息", "自动化", "模式识别", "智能医学")
GENERAL_NOTICE = ("接收推荐免试研究生", "推荐免试研究生预报名", "推荐免试研究生工作办法", "推荐免试研究生招生章程", "推免生预报名", "推荐免试研究生预报名的通知")


@dataclass
class Candidate:
    school: str
    priority: str
    domains: list[str]
    title: str
    url: str


@dataclass
class Notice:
    school: str
    priority: str
    title: str
    url: str
    published: str
    key_times: list[str]
    status: str
    summary: str
    fingerprint: str
    change: str = ""


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def canon(raw: str) -> str:
    raw = clean(raw)
    if raw.startswith("//"):
        raw = "https:" + raw
    p = urlparse(raw)
    if p.scheme not in {"http", "https"} or not p.hostname:
        return ""
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING]
    return urlunparse((p.scheme.lower(), p.hostname.lower().rstrip("."), re.sub(r"/{2,}", "/", p.path or "/"), "", urlencode(query), ""))


def allowed(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def html_url(url: str) -> bool:
    return not urlparse(url).path.lower().endswith(ASSETS)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    return s


def target_year(now: datetime, configured: Any) -> int:
    if isinstance(configured, int) or (isinstance(configured, str) and configured.isdigit()):
        return int(configured)
    return now.year + 1 if now.month >= 3 else now.year


def has_notice(text: str, cfg: dict[str, Any]) -> bool:
    text = clean(text)
    if not any(k in text for k in cfg["notice_keywords"]):
        return False
    excluded = any(k in text for k in cfg["exclude_keywords"])
    receiving = any(k in text for k in ("接收", "预报名", "预推免", "申请", "报名", "夏令营", "开放日"))
    return not (excluded and not receiving)


def headline_notice(text: str, cfg: dict[str, Any]) -> bool:
    text = clean(text)
    if not has_notice(text, cfg) or any(word in text for word in NEWS_RESULTS):
        return False
    if any(word in text for word in EVENT_TERMS) and not any(word in text for word in EVENT_ACTIONS):
        return False
    return True


def scope_ok(school: str, title: str, candidate_title: str, body: str) -> bool:
    headline = clean(f"{candidate_title} {title}")
    lead = clean(body[:1400])
    if any(word in headline for word in TITLE_SCOPE):
        return True
    if school in headline and any(word in headline for word in GENERAL_NOTICE):
        return True
    return any(word in lead for word in TITLE_SCOPE) and any(
        word in headline for word in GENERAL_NOTICE + EVENT_TERMS
    )


def normalize_title(value: str) -> str:
    value = clean(value)
    if " | " in value:
        first = value.split(" | ", 1)[0].strip()
        if headline_fragment(first):
            value = first
    parts = value.split()
    if len(parts) >= 2 and len(parts) % 2 == 0:
        half = len(parts) // 2
        if parts[:half] == parts[half:]:
            value = " ".join(parts[:half])
    for _ in range(2):
        half = len(value) // 2
        if len(value) > 20 and value[:half].strip() == value[half:].strip():
            value = value[:half].strip()
    return value[:180]


def headline_fragment(value: str) -> bool:
    return any(word in value for word in ("推免", "推荐免试", "夏令营", "开放日", "暑期学校", "直博", "招生宣传日"))


def title_signature(value: str) -> str:
    value = normalize_title(value)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value)


def year_in_text(text: str, year: int) -> bool:
    return str(year) in text or str(year - 1) in text


def title_year_ok(title: str, year: int) -> bool:
    years = {int(x) for x in re.findall(r"20\d{2}", title)}
    if not years:
        return True
    early = any(k in title for k in ("夏令营", "开放日", "暑期学校", "招生宣传日", "选拔营"))
    return year in years or (early and year - 1 in years)


def page_title(soup: BeautifulSoup, fallback: str = "") -> str:
    values: list[str] = []
    for selector in (".article-title", ".arti-title", ".news-title", ".content-title", ".title h1", ".title h2", "h1", "h2"):
        for node in soup.select(selector)[:8]:
            value = normalize_title(node.get_text(" ", strip=True))
            if 5 <= len(value) <= 180 and value not in values:
                values.append(value)
    for value in (normalize_title(fallback), normalize_title(soup.title.get_text(" ", strip=True)) if soup.title else ""):
        if value and value not in values:
            values.append(value)
    if not values:
        return ""
    def score(value: str) -> tuple[int, int, int]:
        keywords = sum(k in value for k in ("预推免", "推免", "推荐免试", "夏令营", "开放日", "暑期学校", "直博", "招生宣传日"))
        return keywords, int(bool(re.search(r"20\d{2}", value))), -abs(len(value) - 70)
    return max(values, key=score)


def meta_date(soup: BeautifulSoup) -> str:
    for attr, value in (("property", "article:published_time"), ("name", "publishdate"), ("name", "pubdate"), ("name", "date"), ("itemprop", "datePublished")):
        node = soup.find("meta", attrs={attr: value})
        match = FULL_DATE.search(clean(node.get("content", ""))) if node else None
        if match:
            return f'{int(match["y"]):04d}-{int(match["m"]):02d}-{int(match["d"]):02d}'
    return ""


def fetch(s: requests.Session, url: str, domains: list[str], timeout: int, fallback: str = "") -> tuple[str, str, str, str, BeautifulSoup] | None:
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
    except requests.RequestException:
        return None
    final = canon(r.url)
    if not final or not allowed(final, domains):
        return None
    ctype = r.headers.get("content-type", "").lower()
    if ctype and not any(x in ctype for x in ("html", "xml", "text/plain")):
        return None
    r.encoding = r.apparent_encoding or r.encoding
    soup = BeautifulSoup(r.text, "html.parser")
    title = page_title(soup, fallback)
    published = meta_date(soup)
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe"]):
        tag.decompose()
    body = clean(soup.get_text("\n", strip=True))[:160_000]
    if not published:
        m = FULL_DATE.search(body[:5000])
        if m:
            published = f'{int(m["y"]):04d}-{int(m["m"]):02d}-{int(m["d"]):02d}'
    return final, title, body, published, soup


def nav_score(text: str, url: str) -> int:
    path = urlparse(url).path.lower()
    score = sum(3 for k in NAV_WORDS if k in text) + sum(1 for k in NAV_PATHS if k in path)
    if any(k in text for k in ("计算机", "软件", "人工智能", "网络空间安全", "信息学院")):
        score += 3
    if any(k in text for k in ("研究生招生", "招生信息", "推荐免试", "推免", "夏令营")):
        score += 4
    return score


def school_seeds(school: dict[str, Any], seed_map: dict[str, Any]) -> tuple[list[str], list[str]]:
    raw = list(seed_map.get(school["name"], [])) + list(school.get("seed_urls", []))
    domains = list(school["domains"])
    for value in raw:
        host = (urlparse(canon(value)).hostname or "").lower()
        if host and host not in domains:
            domains.append(host)
    if school.get("crawl_roots", True):
        for domain in school["domains"]:
            raw.extend((f"https://www.{domain}/", f"https://{domain}/"))
    out: list[str] = []
    for value in raw:
        value = canon(value)
        if value and value not in out:
            out.append(value)
    return out, domains


def crawl_school(school: dict[str, Any], seed_map: dict[str, Any], year: int, cfg: dict[str, Any]) -> tuple[list[Candidate], dict[str, Any]]:
    seeds, domains = school_seeds(school, seed_map)
    timeout = int(cfg.get("request_timeout_seconds", 16))
    base_pages = int(cfg.get("max_pages_per_school", 5))
    max_pages = int(school.get("max_crawl_pages", 12 if school.get("priority") == "high" else max(6, base_pages)))
    max_depth = int(school.get("max_crawl_depth", 2))
    max_results = int(cfg.get("max_results_per_school", 14))
    queue: deque[tuple[str, int, int]] = deque((u, 0, 100) for u in seeds)
    seen: set[str] = set()
    candidate_urls: set[str] = set()
    candidates: list[Candidate] = []
    pages = failures = 0
    s = session()
    while queue and pages < max_pages and len(candidates) < max_results:
        frontier = sorted(queue, key=lambda x: (-x[2], x[1], x[0]))
        queue = deque(frontier[1:])
        url, depth, _ = frontier[0]
        url = canon(url)
        if not url or url in seen or not allowed(url, domains) or not html_url(url):
            continue
        seen.add(url)
        doc = fetch(s, url, domains, timeout)
        if doc is None:
            failures += 1
            continue
        final, title, body, _, soup = doc
        pages += 1
        if headline_notice(title, cfg) and year_in_text(f"{title} {body[:12000]}", year) and final not in candidate_urls:
            candidate_urls.add(final)
            candidates.append(Candidate(school["name"], school.get("priority", "normal"), domains, title, final))
        if depth >= max_depth:
            continue
        links: dict[str, tuple[int, str]] = {}
        for a in soup.find_all("a", href=True):
            text = clean(" ".join(filter(None, (a.get_text(" ", strip=True), a.get("title", ""), a.get("aria-label", "")))))
            link = canon(urljoin(final, a.get("href", "")))
            if not link or link in seen or not allowed(link, domains) or not html_url(link):
                continue
            candidate_text = f"{text} {link}"
            if headline_notice(candidate_text, cfg) and (year_in_text(candidate_text, year) or not re.search(r"20\d{2}", candidate_text)) and link not in candidate_urls:
                candidate_urls.add(link)
                candidates.append(Candidate(school["name"], school.get("priority", "normal"), domains, text or "招生通知", link))
                if len(candidates) >= max_results:
                    break
            score = nav_score(text, link)
            if score > 0 and (link not in links or score > links[link][0]):
                links[link] = (score, text)
        for link, (score, _) in sorted(links.items(), key=lambda item: (-item[1][0], item[0]))[:10]:
            queue.append((link, depth + 1, score))
    errors = []
    if pages == 0:
        errors.append("全部入口暂时无法访问")
    elif failures:
        errors.append(f"{failures} 个入口/页面访问失败")
    return candidates, {"seeds": len(seeds), "pages": pages, "failed": failures, "candidates": len(candidates), "notices": 0, "errors": errors}


def key_times(body: str) -> list[str]:
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[。！？；;])|[\r\n]+", body):
        sentence = clean(sentence)
        if not 8 <= len(sentence) <= 340 or not ANY_DATE.search(sentence):
            continue
        if any(k in sentence for k in DEADLINE_WORDS):
            rank = 0
        elif any(k in sentence for k in APPLICATION_WORDS):
            rank = 1
        elif any(k in sentence for k in ASSESSMENT_WORDS):
            rank = 2
        elif any(k in sentence for k in ("时间", "日期", "安排")):
            rank = 4
        else:
            continue
        value = sentence[:260]
        if value not in seen:
            seen.add(value)
            ranked.append((rank, value))
    return [value for _, value in sorted(ranked, key=lambda x: (x[0], len(x[1])))[:5]]


def parse_dates(text: str, default_year: int) -> list[date]:
    values: list[date] = []
    occupied: list[tuple[int, int]] = []
    for m in FULL_DATE.finditer(text):
        try:
            values.append(date(int(m["y"]), int(m["m"]), int(m["d"])))
            occupied.append(m.span())
        except ValueError:
            pass
    for pattern in (MONTH_DAY,):
        for m in pattern.finditer(text):
            if any(a <= m.start() < b for a, b in occupied):
                continue
            try:
                values.append(date(default_year, int(m["m"]), int(m["d"])))
            except ValueError:
                pass
    return values


def notice_status(times: list[str], today: date, published: str) -> str:
    try:
        default_year = date.fromisoformat(published).year
    except ValueError:
        default_year = today.year
    deadlines: list[date] = []
    for value in times:
        explicit = any(k in value for k in DEADLINE_WORDS)
        application_range = any(k in value for k in APPLICATION_WORDS) and any(
            k in value for k in ("至", "到", "前", "之前")
        )
        if explicit or application_range:
            deadlines.extend(parse_dates(value, default_year))
    if not deadlines:
        return "待确认"
    days = (max(deadlines) - today).days
    return "已截止" if days < 0 else ("即将截止" if days <= 3 else "进行中")


def build_notice(candidate: Candidate, year: int, cfg: dict[str, Any], now: datetime) -> Notice | None:
    doc = fetch(session(), candidate.url, candidate.domains, int(cfg.get("request_timeout_seconds", 16)), candidate.title)
    if doc is None:
        return None
    url, title, body, published, _ = doc
    blob = clean(f"{candidate.title} {title} {body[:24000]}")
    headline = clean(f"{candidate.title} {title}")
    if (
        not headline_notice(headline, cfg)
        or not scope_ok(candidate.school, title, candidate.title, body)
        or not year_in_text(blob, year)
        or not title_year_ok(title or candidate.title, year)
    ):
        return None
    if published:
        try:
            if date.fromisoformat(published) < now.date() - timedelta(days=int(cfg.get("lookback_days", 420))) and str(year) not in f"{candidate.title} {title}":
                return None
        except ValueError:
            pass
    times = key_times(body)
    published = published or "未知"
    fingerprint = hashlib.sha256("\n".join([title, url, published, *times]).encode()).hexdigest()
    final_title = normalize_title(title or candidate.title)
    return Notice(candidate.school, candidate.priority, final_title, url, published, times, notice_status(times, now.date(), published), clean(body[:320]), fingerprint)


def notice_key(item: Notice) -> str:
    return hashlib.sha1(f"{item.school}|{canon(item.url)}".encode()).hexdigest()


def md(value: str) -> str:
    return clean(value).replace("|", "\\|")


def sort_key(item: Notice) -> tuple[Any, ...]:
    status = {"即将截止": 0, "进行中": 1, "待确认": 2, "已截止": 3}.get(item.status, 4)
    priority = 0 if item.priority == "high" else 1
    try:
        published = -date.fromisoformat(item.published).toordinal()
    except ValueError:
        published = 0
    return status, priority, published, item.school, item.title


def table(items: list[Notice], limit: int) -> list[str]:
    lines = ["| 变化 | 状态 | 学校 | 通知 | 发布时间 | 关键时间 |", "|---|---|---|---|---|---|"]
    if not items:
        return lines + ["| — | — | — | 暂无 | — | — |"]
    for item in items[:limit]:
        times = "<br>".join(md(x) for x in item.key_times) or "未自动识别，请查看原文"
        lines.append(f"| {md(item.change or '—')} | {item.status} | {md(item.school)} | [{md(item.title)}]({item.url}) | {item.published} | {times} |")
    return lines


def render_report(items: list[Notice], alerts: list[Notice], health: dict[str, dict[str, Any]], now: datetime, year: int) -> str:
    active = [x for x in items if x.status != "已截止"]
    closed = [x for x in items if x.status == "已截止"]
    lines = [
        f"# {year} 计算机类预推免监控日报", "",
        f"- 更新时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 已配置学校/科研单位：{len(health)}",
        f"- 本次访问官网页面：{sum(x['pages'] for x in health.values())}",
        f"- 本次检出有效通知：{len(items)}",
        f"- 新增或关键时间变化：{len(alerts)}",
        "- 数据源：学校研究生院与计算机相关学院官方页面；附件、图片或报名系统中的日期可能无法自动提取。", "",
        "## 今日新增或变更", "", *table(alerts, 60), "",
        "## 当前可关注通知", "", *table(active, 180), "",
        "## 已截止但可能仍有后续安排", "", *table(closed, 100), "",
        "## 抓取状态", "", "| 学校/单位 | 配置入口 | 已访问页面 | 候选链接 | 有效通知 | 状态 |", "|---|---:|---:|---:|---:|---|",
    ]
    for school, info in health.items():
        state = "正常" if not info["errors"] else "；".join(md(x) for x in info["errors"][:2])
        lines.append(f"| {md(school)} | {info['seeds']} | {info['pages']} | {info['candidates']} | {info['notices']} | {state} |")
    lines += ["", "## 状态说明", "", "- `即将截止`：自动识别到的截止日期距离当天不超过 3 天。", "- `待确认`：页面相关，但未可靠识别报名截止日期。", "- 自动提取仅用于提醒；报名资格、材料和日期必须以官网原文及附件为准。", ""]
    return "\n".join(lines)


def render_alerts(items: list[Notice], now: datetime, year: int) -> str:
    if not items:
        return f"# {year} 预推免监控\n\n{now:%Y-%m-%d} 未发现新增或关键时间变化。\n"
    return "\n".join([f"# {year} 计算机类预推免新增/变更", "", f"检测时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}", "", *table(items, 60), "", "> 请点击官网原文核对报名条件、截止时间和附件。", ""])


def output(name: str, value: str) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> int:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    cfg = raw["settings"]
    seed_map = yaml.safe_load(SEEDS.read_text(encoding="utf-8")) if SEEDS.exists() else {}
    seed_map = seed_map.get("schools", seed_map) or {}
    schools = [x for x in raw["schools"] if x.get("enabled", True)]
    now = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Shanghai")))
    year = target_year(now, cfg.get("target_year", "auto"))
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        old = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"items": {}}
    except (OSError, json.JSONDecodeError):
        old = {"items": {}}
    old_items = old.get("items", {}) if old.get("schema_version") == 4 else {}
    first_run = not bool(old_items)

    candidates: list[Candidate] = []
    health: dict[str, dict[str, Any]] = {}
    workers = int(cfg.get("concurrent_workers", 8))
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(crawl_school, school, seed_map, year, cfg): school for school in schools}
        for job in cf.as_completed(jobs):
            school = jobs[job]
            try:
                found, info = job.result()
            except Exception as exc:
                seeds, _ = school_seeds(school, seed_map)
                found, info = [], {"seeds": len(seeds), "pages": 0, "failed": 0, "candidates": 0, "notices": 0, "errors": [f"{type(exc).__name__}: {exc}"]}
            candidates.extend(found)
            health[school["name"]] = info

    unique: dict[str, Candidate] = {}
    for item in candidates:
        key = f"{item.school}|{canon(item.url)}"
        if key not in unique or len(item.title) > len(unique[key].title):
            unique[key] = item
    notices: list[Notice] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(build_notice, item, year, cfg, now): item for item in unique.values()}
        for job in cf.as_completed(jobs):
            candidate = jobs[job]
            try:
                item = job.result()
            except Exception as exc:
                health[candidate.school]["errors"].append(f"通知页解析失败：{type(exc).__name__}")
                continue
            if item:
                notices.append(item)
                health[item.school]["notices"] += 1
    dedup: dict[str, Notice] = {}
    for item in notices:
        key = f"{item.school}|{title_signature(item.title)}"
        if key not in dedup or len(" ".join(item.key_times)) > len(" ".join(dedup[key].key_times)):
            dedup[key] = item
    notices = sorted(dedup.values(), key=sort_key)

    alerts: list[Notice] = []
    state_items = dict(old_items)
    for item in notices:
        key = notice_key(item)
        previous = old_items.get(key)
        item.change = "新增" if previous is None else ("关键时间或标题变化" if previous.get("fingerprint") != item.fingerprint else "")
        if item.change and (not first_run or cfg.get("notify_initial_run", True)):
            alerts.append(item)
        state_items[key] = {**asdict(item), "first_seen": previous.get("first_seen") if previous else now.isoformat(), "last_seen": now.isoformat()}
    alerts.sort(key=sort_key)
    STATE.write_text(json.dumps({"schema_version": 4, "last_run": now.isoformat(), "admission_year": year, "items": state_items}, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT.write_text(render_report(notices, alerts, health, now, year), encoding="utf-8")
    ALERTS.write_text(render_alerts(alerts, now, year), encoding="utf-8")
    output("alert_count", str(len(alerts)))
    output("issue_title", f"[预推免监控] {now:%Y-%m-%d} 新增/变更 {len(alerts)} 条")
    print(f"schools={len(schools)} pages={sum(x['pages'] for x in health.values())} candidates={len(unique)} notices={len(notices)} alerts={len(alerts)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

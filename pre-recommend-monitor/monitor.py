#!/usr/bin/env python3
"""Daily monitor for official CS-related pre-recommendation notices."""

from __future__ import annotations

import base64
import concurrent.futures as futures
import hashlib
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
REPORT_FILE = DATA / "latest.md"
ALERT_FILE = DATA / "alerts.md"
CONFIG_FILE = ROOT / "schools.yml"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 PreRecommendMonitor/2.0"
)
TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "from", "spm", "src", "sessionid",
}
GENERAL_WORDS = (
    "招生简章", "接收办法", "接收章程", "接收推荐免试",
    "推荐免试研究生招生", "各院系", "全校",
)
DEADLINE_WORDS = ("截止", "结束", "关闭", "逾期", "最后", "截至")
APPLICATION_WORDS = ("报名", "申请", "提交", "填报", "材料", "系统开放", "注册")
ASSESSMENT_WORDS = ("复试", "面试", "考核", "选拔", "宣讲", "入营", "报到", "确认")
FULL_DATE = re.compile(
    r"(?P<y>20\d{2})[年./-]\s*(?P<m>\d{1,2})[月./-]\s*(?P<d>\d{1,2})(?:日|号)?"
)
MONTH_DAY = re.compile(r"(?<!\d)(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})(?:日|号)?")
ANY_DATE = re.compile(r"(?:20\d{2}[年./-]\s*)?\d{1,2}[月./-]\s*\d{1,2}(?:日|号)?")
SEARCH_HOSTS = ("bing.com", "duckduckgo.com")


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


def text_from_html(value: str) -> str:
    return clean(BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True))


def canonical(raw: str) -> str:
    raw = clean(raw)
    if raw.startswith("//"):
        raw = "https:" + raw
    p = urlparse(raw)
    host = (p.hostname or "").lower().rstrip(".")
    query = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING
    ]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    scheme = "https" if p.scheme in {"http", "https"} else p.scheme
    return urlunparse((scheme, host, path, "", urlencode(query), ""))


def official(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def is_search_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in SEARCH_HOSTS)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def admission_year(now: datetime, configured: Any) -> int:
    if isinstance(configured, int) or (isinstance(configured, str) and configured.isdigit()):
        return int(configured)
    return now.year + 1 if now.month >= 3 else now.year


def search_queries(domain: str, year: int) -> list[str]:
    # Keep queries deliberately simple. Complex OR/parenthesized Bing RSS
    # queries often return an empty feed even when indexed pages exist.
    return [
        f"site:{domain} {year} 推免",
        f"site:{domain} {year} 推荐免试 计算机",
        f"site:{domain} {year} 夏令营 计算机",
    ]


def preliminary_relevant(text: str, cfg: dict[str, Any]) -> bool:
    has_notice = any(x in text for x in cfg["notice_keywords"])
    excluded = any(x in text for x in cfg["exclude_keywords"])
    receiving = any(x in text for x in ("接收", "预报名", "预推免", "申请", "报名", "夏令营"))
    return has_notice and not (excluded and not receiving)


def fully_relevant(text: str, cfg: dict[str, Any]) -> bool:
    return preliminary_relevant(text, cfg) and (
        any(x in text for x in cfg["major_keywords"])
        or any(x in text for x in GENERAL_WORDS)
    )


def unwrap_search_url(raw: str) -> str:
    """Decode common Bing/DDG tracking URLs into the destination URL."""
    raw = html.unescape(clean(raw))
    if raw.startswith("//"):
        raw = "https:" + raw
    p = urlparse(raw)
    host = (p.hostname or "").lower()
    qs = parse_qs(p.query)

    if host.endswith("duckduckgo.com"):
        value = qs.get("uddg", [""])[0]
        if value:
            return canonical(unquote(value))

    if host.endswith("bing.com") and p.path.startswith("/ck/"):
        value = qs.get("u", [""])[0]
        if value:
            value = unquote(value)
            if value.startswith(("http://", "https://")):
                return canonical(value)
            # Bing commonly prefixes a URL-safe base64 value with "a1".
            payload = value[2:] if value.startswith("a1") else value
            try:
                payload += "=" * (-len(payload) % 4)
                decoded = base64.urlsafe_b64decode(payload).decode("utf-8", errors="ignore")
                if decoded.startswith(("http://", "https://")):
                    return canonical(decoded)
            except Exception:
                pass
    return canonical(raw)


def resolve_search_url(
    s: requests.Session,
    raw: str,
    domains: list[str],
    timeout: int,
) -> str:
    url = unwrap_search_url(raw)
    if official(url, domains):
        return url
    if not is_search_url(url):
        return ""
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True, stream=True)
        final = unwrap_search_url(r.url)
        r.close()
        return final if official(final, domains) else ""
    except requests.RequestException:
        return ""


def candidate_record(
    school: dict[str, Any],
    title: str,
    url: str,
    summary: str = "",
    rss_date: str = "",
) -> dict[str, Any]:
    return {
        "school": school["name"],
        "priority": school.get("priority", "normal"),
        "domains": school["domains"],
        "title": clean(title),
        "url": canonical(url),
        "summary": clean(summary),
        "rss_date": clean(rss_date),
    }


def parse_bing_rss(
    s: requests.Session,
    school: dict[str, Any],
    query: str,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    r = s.get(
        "https://www.bing.com/search",
        params={"q": query, "format": "rss", "setlang": "zh-Hans", "cc": "cn"},
        timeout=cfg["request_timeout_seconds"],
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    nodes = root.findall(".//item")
    if school["name"] == "中山大学" and nodes:
        first = nodes[0]
        print(
            "DEBUG_BING_RSS",
            query,
            repr(clean(first.findtext("title", ""))),
            repr(clean(first.findtext("link", ""))),
        )
    results: list[dict[str, Any]] = []
    for item in nodes:
        title = clean(item.findtext("title", ""))
        raw_url = clean(item.findtext("link", ""))
        summary = text_from_html(item.findtext("description", ""))
        url = resolve_search_url(
            s, raw_url, school["domains"], cfg["request_timeout_seconds"]
        )
        if title and url:
            results.append(candidate_record(
                school, title, url, summary, item.findtext("pubDate", "")
            ))
    return results, len(nodes)


def parse_bing_html(
    s: requests.Session,
    school: dict[str, Any],
    query: str,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    r = s.get(
        "https://www.bing.com/search",
        params={"q": query, "setlang": "zh-Hans", "cc": "cn", "count": 20},
        timeout=cfg["request_timeout_seconds"],
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    nodes = soup.select("li.b_algo")
    results: list[dict[str, Any]] = []
    for node in nodes:
        anchor = node.select_one("h2 a")
        if not anchor:
            continue
        title = clean(anchor.get_text(" ", strip=True))
        summary_node = node.select_one(".b_caption p")
        summary = clean(summary_node.get_text(" ", strip=True) if summary_node else "")
        url = resolve_search_url(
            s, anchor.get("href", ""), school["domains"], cfg["request_timeout_seconds"]
        )
        if title and url:
            results.append(candidate_record(school, title, url, summary))
    return results, len(nodes)


def parse_ddg_html(
    s: requests.Session,
    school: dict[str, Any],
    query: str,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    r = s.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        timeout=cfg["request_timeout_seconds"],
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    nodes = soup.select(".result")
    results: list[dict[str, Any]] = []
    for node in nodes:
        anchor = node.select_one("a.result__a")
        if not anchor:
            continue
        title = clean(anchor.get_text(" ", strip=True))
        summary_node = node.select_one(".result__snippet")
        summary = clean(summary_node.get_text(" ", strip=True) if summary_node else "")
        url = resolve_search_url(
            s, anchor.get("href", ""), school["domains"], cfg["request_timeout_seconds"]
        )
        if title and url:
            results.append(candidate_record(school, title, url, summary))
    return results, len(nodes)


def seed_candidates(
    s: requests.Session,
    school: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read configured official list pages and collect relevant article links."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for seed in school.get("seed_urls", []):
        try:
            r = s.get(seed, timeout=cfg["request_timeout_seconds"], allow_redirects=True)
            r.raise_for_status()
            final = canonical(r.url)
            if not official(final, school["domains"]):
                raise ValueError(f"redirected outside official domains: {final}")
            r.encoding = r.apparent_encoding or r.encoding
            soup = BeautifulSoup(r.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                title = clean(anchor.get_text(" ", strip=True))
                if not title or not preliminary_relevant(title, cfg):
                    continue
                url = canonical(urljoin(final, anchor["href"]))
                if official(url, school["domains"]):
                    results.append(candidate_record(school, title, url))
        except Exception as exc:
            errors.append(f"seed {seed}: {type(exc).__name__}: {exc}")
    return results, errors


def search_candidates(
    school: dict[str, Any],
    year: int,
    cfg: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    stats = {"rss_items": 0, "html_items": 0, "ddg_items": 0, "seed_links": 0}
    seen: set[str] = set()
    s = session()

    seeded, seed_errors = seed_candidates(s, school, cfg)
    errors.extend(seed_errors)
    stats["seed_links"] = len(seeded)
    results.extend(seeded)

    for domain in school["domains"]:
        queries = search_queries(domain, year)
        for query in queries:
            try:
                found, raw_count = parse_bing_rss(s, school, query, cfg)
                stats["rss_items"] += raw_count
                results.extend(found)
            except Exception as exc:
                errors.append(f"bing-rss {domain}: {type(exc).__name__}: {exc}")

        # HTML search is a fallback because RSS can legally return an empty feed.
        if not any(official(x["url"], [domain]) for x in results):
            try:
                found, raw_count = parse_bing_html(s, school, queries[0], cfg)
                stats["html_items"] += raw_count
                results.extend(found)
            except Exception as exc:
                errors.append(f"bing-html {domain}: {type(exc).__name__}: {exc}")

        if not any(official(x["url"], [domain]) for x in results):
            try:
                found, raw_count = parse_ddg_html(
                    s, school, f"site:{domain} {year} 推免 计算机", cfg
                )
                stats["ddg_items"] += raw_count
                results.extend(found)
            except Exception as exc:
                errors.append(f"ddg {domain}: {type(exc).__name__}: {exc}")

    filtered: list[dict[str, Any]] = []
    for item in results:
        url = canonical(item["url"])
        if not url or not official(url, school["domains"]) or url in seen:
            continue
        # Search summaries sometimes omit the crucial wording, so retain any
        # official result returned by a tightly-scoped query and verify after fetch.
        seen.add(url)
        item["url"] = url
        filtered.append(item)
        if len(filtered) >= cfg["max_results_per_school"]:
            break

    return filtered, errors, stats


def meta_date(soup: BeautifulSoup) -> str:
    for attrs in (
        {"property": "article:published_time"},
        {"name": "publishdate"},
        {"name": "pubdate"},
        {"name": "date"},
        {"itemprop": "datePublished"},
    ):
        node = soup.find("meta", attrs=attrs)
        match = FULL_DATE.search(clean(node.get("content", ""))) if node else None
        if match:
            return f'{int(match["y"]):04d}-{int(match["m"]):02d}-{int(match["d"]):02d}'
    return ""


def fetch_page(
    candidate: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[str, str, str, str]:
    title = candidate["title"]
    url = candidate["url"]
    body = candidate["summary"]
    published = ""
    try:
        r = session().get(
            url,
            timeout=cfg["request_timeout_seconds"],
            allow_redirects=True,
        )
        r.raise_for_status()
        final = canonical(r.url)
        if not official(final, candidate["domains"]):
            return title, url, body, published
        url = final
        content_type = r.headers.get("content-type", "").lower()
        if "html" in content_type or not content_type:
            r.encoding = r.apparent_encoding or r.encoding
            soup = BeautifulSoup(r.text, "html.parser")
            published = meta_date(soup)
            page_title = clean(soup.title.get_text(" ", strip=True) if soup.title else "")
            if page_title and len(page_title) <= 180:
                title = page_title
            for tag in soup([
                "script", "style", "noscript", "svg", "canvas", "iframe", "nav", "footer"
            ]):
                tag.decompose()
            body = clean(soup.get_text("\n", strip=True))[:120_000]
            if not published:
                match = FULL_DATE.search(body[:3500])
                if match:
                    published = (
                        f'{int(match["y"]):04d}-'
                        f'{int(match["m"]):02d}-'
                        f'{int(match["d"]):02d}'
                    )
    except requests.RequestException:
        pass

    if not published and candidate.get("rss_date"):
        try:
            published = parsedate_to_datetime(candidate["rss_date"]).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OverflowError):
            pass
    return title, url, body, published


def key_times(body: str) -> list[str]:
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[。！？；;])|[\r\n]+", body):
        sentence = clean(sentence)
        if not 8 <= len(sentence) <= 300 or not ANY_DATE.search(sentence):
            continue
        if any(x in sentence for x in DEADLINE_WORDS):
            rank = 0
        elif any(x in sentence for x in APPLICATION_WORDS):
            rank = 1
        elif any(x in sentence for x in ASSESSMENT_WORDS):
            rank = 2
        elif any(x in sentence for x in ("时间", "日期", "安排")):
            rank = 4
        else:
            continue
        sentence = sentence[:240]
        if sentence not in seen:
            seen.add(sentence)
            ranked.append((rank, sentence))
    ranked.sort(key=lambda x: (x[0], len(x[1])))
    return [x[1] for x in ranked[:5]]


def parsed_dates(text: str, default_year: int) -> list[date]:
    values: list[date] = []
    spans: list[tuple[int, int]] = []
    for match in FULL_DATE.finditer(text):
        try:
            values.append(date(
                int(match["y"]), int(match["m"]), int(match["d"])
            ))
            spans.append(match.span())
        except ValueError:
            pass
    for match in MONTH_DAY.finditer(text):
        if any(a <= match.start() < b for a, b in spans):
            continue
        try:
            values.append(date(
                default_year, int(match["m"]), int(match["d"])
            ))
        except ValueError:
            pass
    return values


def status_for(times: list[str], today: date) -> str:
    deadlines: list[date] = []
    for value in times:
        if any(x in value for x in DEADLINE_WORDS):
            deadlines.extend(parsed_dates(value, today.year))
    if not deadlines:
        return "待确认"
    remaining = (max(deadlines) - today).days
    if remaining < 0:
        return "已截止"
    return "即将截止" if remaining <= 3 else "进行中"


def build_notice(
    candidate: dict[str, Any],
    cfg: dict[str, Any],
    now: datetime,
    year: int,
) -> Notice | None:
    title, url, body, published = fetch_page(candidate, cfg)
    blob = clean(
        f"{candidate['title']} {title} {candidate['summary']} {body[:16000]}"
    )
    if not fully_relevant(blob, cfg):
        return None

    # Reject clearly unrelated admission years. Keep undated list pages so they
    # can still surface a current article if the page text contains the target year.
    if str(year) not in blob and str(year - 1) not in blob:
        return None
    if published:
        try:
            if (
                date.fromisoformat(published)
                < now.date() - timedelta(days=cfg["lookback_days"])
                and str(year) not in blob
            ):
                return None
        except ValueError:
            pass

    times = key_times(body)
    fingerprint = hashlib.sha256(
        "\n".join([title, url, published, *times]).encode()
    ).hexdigest()
    display_title = candidate["title"]
    if title and len(title) < len(display_title):
        display_title = title

    return Notice(
        school=candidate["school"],
        priority=candidate["priority"],
        title=clean(display_title),
        url=url,
        published=published or "未知",
        key_times=times,
        status=status_for(times, now.date()),
        summary=clean(candidate["summary"] or body[:260])[:260],
        fingerprint=fingerprint,
    )


def notice_key(item: Notice) -> str:
    return hashlib.sha1(
        f"{item.school}|{canonical(item.url)}".encode()
    ).hexdigest()


def md(value: str) -> str:
    return clean(value).replace("|", "\\|")


def sort_key(item: Notice) -> tuple[Any, ...]:
    status_rank = {
        "即将截止": 0,
        "进行中": 1,
        "待确认": 2,
        "已截止": 3,
    }.get(item.status, 4)
    priority_rank = 0 if item.priority == "high" else 1
    try:
        published_rank = -date.fromisoformat(item.published).toordinal()
    except ValueError:
        published_rank = 0
    return status_rank, priority_rank, published_rank, item.school, item.title


def table(items: list[Notice], limit: int) -> list[str]:
    lines = [
        "| 状态 | 学校 | 通知 | 发布时间 | 关键时间 |",
        "|---|---|---|---|---|",
    ]
    if not items:
        return lines + ["| — | — | 暂无 | — | — |"]
    for item in items[:limit]:
        times = "<br>".join(md(x) for x in item.key_times)
        if not times:
            times = "未自动识别，请查看原文"
        lines.append(
            f"| {item.status} | {md(item.school)} | "
            f"[{md(item.title)}]({item.url}) | {item.published} | {times} |"
        )
    return lines


def report(
    items: list[Notice],
    alerts: list[Notice],
    health: dict[str, Any],
    now: datetime,
    year: int,
) -> str:
    active = [x for x in items if x.status != "已截止"]
    closed = [x for x in items if x.status == "已截止"]
    lines = [
        f"# {year} 计算机类预推免监控日报",
        "",
        f"- 更新时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 已监测学校/单位：{len(health)}",
        f"- 本次检出通知：{len(items)}",
        f"- 新增或关键时间变化：{len(alerts)}",
        "- 数据源：学校官方域名限定检索、官网列表页和官网原文；"
        "附件日期可能漏提取，请以官网原文为准。",
        "",
        "## 今日新增或变更",
        "",
        *table(alerts, 50),
        "",
        "## 当前可关注通知",
        "",
        *table(active, 150),
        "",
        "## 已截止但可能仍有后续安排",
        "",
        *table(closed, 80),
        "",
        "## 抓取状态",
        "",
        "| 学校/单位 | 搜索原始结果 | 官网候选 | 有效通知 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    for school, info in health.items():
        raw = (
            info["stats"]["rss_items"]
            + info["stats"]["html_items"]
            + info["stats"]["ddg_items"]
            + info["stats"]["seed_links"]
        )
        err = "；".join(info["errors"])
        state = "正常" if not err else f"部分异常：{md(err[:180])}"
        lines.append(
            f"| {md(school)} | {raw} | {info['candidates']} | "
            f"{info['notices']} | {state} |"
        )
    lines += [
        "",
        "## 状态说明",
        "",
        "- `即将截止`：识别到的截止日期不超过 3 天。",
        "- `待确认`：页面相关，但未可靠识别截止日期。",
        "",
    ]
    return "\n".join(lines)


def alert_report(
    alerts: list[Notice],
    now: datetime,
    year: int,
) -> str:
    if not alerts:
        return (
            f"# {year} 预推免监控\n\n"
            f"{now:%Y-%m-%d} 未发现新增或关键时间变化。\n"
        )
    return "\n".join([
        f"# {year} 计算机类预推免新增/变更",
        "",
        f"检测时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        *table(alerts, 50),
        "",
        "> 请点击官网原文核对报名条件、截止时间和附件。",
        "",
    ])


def output(name: str, value: str) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> int:
    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg = raw["settings"]
    for key in (
        "lookback_days",
        "max_results_per_school",
        "max_pages_per_school",
        "request_timeout_seconds",
        "concurrent_workers",
    ):
        cfg[key] = int(cfg[key])

    schools = [x for x in raw["schools"] if x.get("enabled", True)]
    now = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Shanghai")))
    year = admission_year(now, cfg.get("target_year", "auto"))
    DATA.mkdir(parents=True, exist_ok=True)

    try:
        old = (
            json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if STATE_FILE.exists()
            else {"items": {}}
        )
    except (OSError, json.JSONDecodeError):
        old = {"items": {}}
    old_items = old.get("items", {})
    first_run = not bool(old_items)

    health = {
        x["name"]: {
            "candidates": 0,
            "notices": 0,
            "errors": [],
            "stats": {
                "rss_items": 0,
                "html_items": 0,
                "ddg_items": 0,
                "seed_links": 0,
            },
        }
        for x in schools
    }

    candidates: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(
        max_workers=cfg["concurrent_workers"]
    ) as pool:
        jobs = {
            pool.submit(search_candidates, school, year, cfg): school
            for school in schools
        }
        for job in futures.as_completed(jobs):
            school = jobs[job]
            try:
                found, errors, stats = job.result()
            except Exception as exc:
                found = []
                errors = [f"{type(exc).__name__}: {exc}"]
                stats = health[school["name"]]["stats"]
            health[school["name"]]["candidates"] = len(found)
            health[school["name"]]["errors"] += errors
            health[school["name"]]["stats"] = stats
            candidates += found[: cfg["max_pages_per_school"]]

    unique = {
        f"{x['school']}|{canonical(x['url'])}": x
        for x in candidates
    }

    notices: list[Notice] = []
    with futures.ThreadPoolExecutor(
        max_workers=cfg["concurrent_workers"]
    ) as pool:
        jobs = {
            pool.submit(build_notice, item, cfg, now, year): item
            for item in unique.values()
        }
        for job in futures.as_completed(jobs):
            candidate = jobs[job]
            try:
                item = job.result()
            except Exception as exc:
                health[candidate["school"]]["errors"].append(
                    f"page: {type(exc).__name__}: {exc}"
                )
                continue
            if item:
                notices.append(item)
                health[item.school]["notices"] += 1

    deduped: dict[str, Notice] = {}
    for item in notices:
        key = notice_key(item)
        if (
            key not in deduped
            or len(" ".join(item.key_times))
            > len(" ".join(deduped[key].key_times))
        ):
            deduped[key] = item
    notices = sorted(deduped.values(), key=sort_key)

    alerts: list[Notice] = []
    state_items = dict(old_items)
    for item in notices:
        key = notice_key(item)
        previous = old_items.get(key)
        if previous is None:
            item.change = "新增"
        elif previous.get("fingerprint") != item.fingerprint:
            item.change = "关键时间或标题变化"
        if (
            item.change
            and (not first_run or cfg.get("notify_initial_run", True))
        ):
            alerts.append(item)
        state_items[key] = {
            **asdict(item),
            "first_seen": (
                previous.get("first_seen")
                if previous
                else now.isoformat()
            ),
            "last_seen": now.isoformat(),
        }
    alerts.sort(key=sort_key)

    STATE_FILE.write_text(
        json.dumps({
            "schema_version": 2,
            "last_run": now.isoformat(),
            "admission_year": year,
            "items": state_items,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    REPORT_FILE.write_text(
        report(notices, alerts, health, now, year),
        encoding="utf-8",
    )
    ALERT_FILE.write_text(
        alert_report(alerts, now, year),
        encoding="utf-8",
    )
    output("alert_count", str(len(alerts)))
    output(
        "issue_title",
        f"[预推免监控] {now:%Y-%m-%d} 新增/变更 {len(alerts)} 条",
    )
    raw_count = sum(
        sum(x["stats"].values()) for x in health.values()
    )
    print(
        f"schools={len(schools)} raw_results={raw_count} "
        f"candidates={len(unique)} notices={len(notices)} alerts={len(alerts)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

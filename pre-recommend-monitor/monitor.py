#!/usr/bin/env python3
"""Daily monitor for official CS-related pre-recommendation notices."""

from __future__ import annotations

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
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
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
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 PreRecommendMonitor/1.0"
)
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "from", "spm", "src"}
NOTICE_QUERY = ("预推免", "推免预报名", "接收推荐免试", "推荐免试研究生", "校园开放日", "优秀大学生", "夏令营")
MAJOR_QUERY = ("计算机", "软件", "人工智能", "网络空间安全", "数据科学", "电子信息")
GENERAL_WORDS = ("招生简章", "接收办法", "接收章程", "接收推荐免试", "推荐免试研究生招生", "各院系", "全校")
DEADLINE_WORDS = ("截止", "结束", "关闭", "逾期", "最后")
APPLICATION_WORDS = ("报名", "申请", "提交", "填报", "材料", "系统开放", "注册")
ASSESSMENT_WORDS = ("复试", "面试", "考核", "选拔", "宣讲", "入营", "报到", "确认")
FULL_DATE = re.compile(r"(?P<y>20\d{2})[年./-]\s*(?P<m>\d{1,2})[月./-]\s*(?P<d>\d{1,2})(?:日|号)?")
MONTH_DAY = re.compile(r"(?<!\d)(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})(?:日|号)?")
ANY_DATE = re.compile(r"(?:20\d{2}[年./-]\s*)?\d{1,2}[月./-]\s*\d{1,2}(?:日|号)?")


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
    p = urlparse(raw.strip())
    host = (p.hostname or "").lower().rstrip(".")
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return urlunparse(("https" if p.scheme in {"http", "https"} else p.scheme, host, path, "", urlencode(query), ""))


def official(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})
    return s


def admission_year(now: datetime, configured: Any) -> int:
    if isinstance(configured, int) or (isinstance(configured, str) and configured.isdigit()):
        return int(configured)
    return now.year + 1 if now.month >= 3 else now.year


def query_for(domain: str, year: int) -> str:
    notices = " OR ".join(f'"{x}"' for x in NOTICE_QUERY)
    majors = " OR ".join(f'"{x}"' for x in MAJOR_QUERY)
    return f"site:{domain} ({notices}) ({majors}) ({year} OR {year - 1})"


def preliminary_relevant(text: str, cfg: dict[str, Any]) -> bool:
    has_notice = any(x in text for x in cfg["notice_keywords"])
    excluded = any(x in text for x in cfg["exclude_keywords"])
    receiving = any(x in text for x in ("接收", "预报名", "预推免", "申请", "报名"))
    return has_notice and not (excluded and not receiving)


def fully_relevant(text: str, cfg: dict[str, Any]) -> bool:
    return preliminary_relevant(text, cfg) and (
        any(x in text for x in cfg["major_keywords"]) or any(x in text for x in GENERAL_WORDS)
    )


def bing_candidates(school: dict[str, Any], year: int, cfg: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    results: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    s = session()
    for domain in school["domains"]:
        try:
            r = s.get(
                "https://www.bing.com/search",
                params={"q": query_for(domain, year), "format": "rss", "setlang": "zh-Hans", "cc": "cn"},
                timeout=cfg["request_timeout_seconds"],
            )
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as exc:
            errors.append(f"{domain}: {type(exc).__name__}: {exc}")
            continue
        for item in root.findall(".//item"):
            title = clean(item.findtext("title", ""))
            url = canonical(clean(item.findtext("link", "")))
            summary = text_from_html(item.findtext("description", ""))
            if not title or not url or not official(url, school["domains"]):
                continue
            if url in seen or not preliminary_relevant(f"{title} {summary}", cfg):
                continue
            seen.add(url)
            results.append(
                {
                    "school": school["name"],
                    "priority": school.get("priority", "normal"),
                    "domains": school["domains"],
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "rss_date": clean(item.findtext("pubDate", "")),
                }
            )
            if len(results) >= cfg["max_results_per_school"]:
                break
    return results, errors


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


def fetch_page(candidate: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str, str, str]:
    title, url, body, published = candidate["title"], candidate["url"], candidate["summary"], ""
    try:
        r = session().get(url, timeout=cfg["request_timeout_seconds"], allow_redirects=True)
        r.raise_for_status()
        final = canonical(r.url)
        if not official(final, candidate["domains"]):
            return title, url, body, published
        url = final
        if "html" in r.headers.get("content-type", "").lower() or not r.headers.get("content-type"):
            r.encoding = r.apparent_encoding or r.encoding
            soup = BeautifulSoup(r.text, "html.parser")
            published = meta_date(soup)
            page_title = clean(soup.title.get_text(" ", strip=True) if soup.title else "")
            if page_title and len(page_title) <= 180:
                title = page_title
            for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe", "nav", "footer"]):
                tag.decompose()
            body = clean(soup.get_text("\n", strip=True))[:120_000]
            if not published:
                match = FULL_DATE.search(body[:2500])
                if match:
                    published = f'{int(match["y"]):04d}-{int(match["m"]):02d}-{int(match["d"]):02d}'
    except requests.RequestException:
        pass
    if not published and candidate["rss_date"]:
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
        if not 8 <= len(sentence) <= 260 or not ANY_DATE.search(sentence):
            continue
        rank = 9
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
        sentence = sentence[:220]
        if sentence not in seen:
            seen.add(sentence)
            ranked.append((rank, sentence))
    ranked.sort(key=lambda x: (x[0], len(x[1])))
    return [x[1] for x in ranked[:4]]


def parsed_dates(text: str, default_year: int) -> list[date]:
    values: list[date] = []
    spans: list[tuple[int, int]] = []
    for match in FULL_DATE.finditer(text):
        try:
            values.append(date(int(match["y"]), int(match["m"]), int(match["d"])))
            spans.append(match.span())
        except ValueError:
            pass
    for match in MONTH_DAY.finditer(text):
        if any(a <= match.start() < b for a, b in spans):
            continue
        try:
            values.append(date(default_year, int(match["m"]), int(match["d"])))
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
    return "已截止" if remaining < 0 else ("即将截止" if remaining <= 3 else "进行中")


def build_notice(candidate: dict[str, Any], cfg: dict[str, Any], now: datetime, year: int) -> Notice | None:
    title, url, body, published = fetch_page(candidate, cfg)
    blob = clean(f"{candidate['title']} {title} {candidate['summary']} {body[:12000]}")
    if not fully_relevant(blob, cfg):
        return None
    if published:
        try:
            if date.fromisoformat(published) < now.date() - timedelta(days=cfg["lookback_days"]) and str(year) not in blob:
                return None
        except ValueError:
            pass
    times = key_times(body)
    fingerprint = hashlib.sha256("\n".join([title, url, published, *times]).encode()).hexdigest()
    return Notice(
        school=candidate["school"],
        priority=candidate["priority"],
        title=clean(candidate["title"] if len(candidate["title"]) <= len(title) or not title else title),
        url=url,
        published=published or "未知",
        key_times=times,
        status=status_for(times, now.date()),
        summary=clean(candidate["summary"] or body[:260])[:260],
        fingerprint=fingerprint,
    )


def notice_key(item: Notice) -> str:
    return hashlib.sha1(f"{item.school}|{canonical(item.url)}".encode()).hexdigest()


def md(value: str) -> str:
    return clean(value).replace("|", "\\|")


def sort_key(item: Notice) -> tuple[Any, ...]:
    status_rank = {"即将截止": 0, "进行中": 1, "待确认": 2, "已截止": 3}.get(item.status, 4)
    priority_rank = 0 if item.priority == "high" else 1
    try:
        published_rank = -date.fromisoformat(item.published).toordinal()
    except ValueError:
        published_rank = 0
    return status_rank, priority_rank, published_rank, item.school, item.title


def table(items: list[Notice], limit: int) -> list[str]:
    lines = ["| 状态 | 学校 | 通知 | 发布时间 | 关键时间 |", "|---|---|---|---|---|"]
    if not items:
        return lines + ["| — | — | 暂无 | — | — |"]
    for item in items[:limit]:
        times = "<br>".join(md(x) for x in item.key_times) or "未自动识别，请查看原文"
        lines.append(f"| {item.status} | {md(item.school)} | [{md(item.title)}]({item.url}) | {item.published} | {times} |")
    return lines


def report(items: list[Notice], alerts: list[Notice], health: dict[str, Any], now: datetime, year: int) -> str:
    active = [x for x in items if x.status != "已截止"]
    closed = [x for x in items if x.status == "已截止"]
    lines = [
        f"# {year} 计算机类预推免监控日报", "",
        f"- 更新时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 已监测学校/单位：{len(health)}",
        f"- 本次检出通知：{len(items)}",
        f"- 新增或关键时间变化：{len(alerts)}",
        "- 数据源：学校官方域名限定搜索与官网原文；附件日期可能漏提取，请以官网原文为准。", "",
        "## 今日新增或变更", "", *table(alerts, 40), "",
        "## 当前可关注通知", "", *table(active, 120), "",
        "## 已截止但可能仍有后续安排", "", *table(closed, 60), "",
        "## 抓取状态", "", "| 学校/单位 | 候选结果 | 有效通知 | 状态 |", "|---|---:|---:|---|",
    ]
    for school, info in health.items():
        err = "；".join(info["errors"])
        state = "正常" if not err else f"部分异常：{md(err[:180])}"
        lines.append(f"| {md(school)} | {info['candidates']} | {info['notices']} | {state} |")
    lines += ["", "## 状态说明", "", "- `即将截止`：识别到的截止日期不超过 3 天。", "- `待确认`：页面相关，但未可靠识别截止日期。", ""]
    return "\n".join(lines)


def alert_report(alerts: list[Notice], now: datetime, year: int) -> str:
    if not alerts:
        return f"# {year} 预推免监控\n\n{now:%Y-%m-%d} 未发现新增或关键时间变化。\n"
    return "\n".join([
        f"# {year} 计算机类预推免新增/变更", "", f"检测时间：{now.strftime('%Y-%m-%d %H:%M:%S %Z')}", "",
        *table(alerts, 40), "", "> 请点击官网原文核对报名条件、截止时间和附件。", "",
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
    for key in ("lookback_days", "max_results_per_school", "max_pages_per_school", "request_timeout_seconds", "concurrent_workers"):
        cfg[key] = int(cfg[key])
    schools = [x for x in raw["schools"] if x.get("enabled", True)]
    now = datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Shanghai")))
    year = admission_year(now, cfg.get("target_year", "auto"))
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        old = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"items": {}}
    except (OSError, json.JSONDecodeError):
        old = {"items": {}}
    old_items = old.get("items", {})
    first_run = not bool(old_items)
    health = {x["name"]: {"candidates": 0, "notices": 0, "errors": []} for x in schools}

    candidates: list[dict[str, Any]] = []
    with futures.ThreadPoolExecutor(max_workers=cfg["concurrent_workers"]) as pool:
        jobs = {pool.submit(bing_candidates, school, year, cfg): school for school in schools}
        for job in futures.as_completed(jobs):
            school = jobs[job]
            try:
                found, errors = job.result()
            except Exception as exc:
                found, errors = [], [f"{type(exc).__name__}: {exc}"]
            health[school["name"]]["candidates"] = len(found)
            health[school["name"]]["errors"] += errors
            candidates += found[: cfg["max_pages_per_school"]]

    unique = {f"{x['school']}|{canonical(x['url'])}": x for x in candidates}
    notices: list[Notice] = []
    with futures.ThreadPoolExecutor(max_workers=cfg["concurrent_workers"]) as pool:
        jobs = {pool.submit(build_notice, x, cfg, now, year): x for x in unique.values()}
        for job in futures.as_completed(jobs):
            candidate = jobs[job]
            try:
                item = job.result()
            except Exception as exc:
                health[candidate["school"]]["errors"].append(f"page: {type(exc).__name__}: {exc}")
                continue
            if item:
                notices.append(item)
                health[item.school]["notices"] += 1

    deduped: dict[str, Notice] = {}
    for item in notices:
        key = notice_key(item)
        if key not in deduped or len(" ".join(item.key_times)) > len(" ".join(deduped[key].key_times)):
            deduped[key] = item
    notices = sorted(deduped.values(), key=sort_key)

    alerts: list[Notice] = []
    state_items = dict(old_items)
    for item in notices:
        key = notice_key(item)
        previous = old_items.get(key)
        item.change = "新增" if previous is None else ("关键时间或标题变化" if previous.get("fingerprint") != item.fingerprint else "")
        if item.change and (not first_run or cfg.get("notify_initial_run", True)):
            alerts.append(item)
        state_items[key] = {
            **asdict(item),
            "first_seen": previous.get("first_seen") if previous else now.isoformat(),
            "last_seen": now.isoformat(),
        }
    alerts.sort(key=sort_key)

    STATE_FILE.write_text(json.dumps({"schema_version": 1, "last_run": now.isoformat(), "admission_year": year, "items": state_items}, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_FILE.write_text(report(notices, alerts, health, now, year), encoding="utf-8")
    ALERT_FILE.write_text(alert_report(alerts, now, year), encoding="utf-8")
    output("alert_count", str(len(alerts)))
    output("issue_title", f"[预推免监控] {now:%Y-%m-%d} 新增/变更 {len(alerts)} 条")
    print(f"schools={len(schools)} candidates={len(unique)} notices={len(notices)} alerts={len(alerts)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

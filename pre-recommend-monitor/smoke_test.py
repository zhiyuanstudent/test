#!/usr/bin/env python3
"""Regression checks against known official 2027 pre-recommendation notices."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

import monitor_v3 as monitor

CASES = [
    monitor.Candidate(
        school="厦门大学",
        priority="high",
        domains=["xmu.edu.cn"],
        title="厦门大学信息学院关于2027年接收推荐免试研究生预报名的通知",
        url="https://informatics.xmu.edu.cn/info/1072/202371.htm",
    ),
    monitor.Candidate(
        school="南开大学",
        priority="high",
        domains=["nankai.edu.cn"],
        title="南开大学软件学院2027年推荐免试研究生预报名通知",
        url="https://cs.nankai.edu.cn/info/1076/3673.htm",
    ),
    monitor.Candidate(
        school="中山大学",
        priority="high",
        domains=["sysu.edu.cn"],
        title="中山大学计算机学院关于接收2027年推荐免试研究生预报名的通知",
        url="https://cse.sysu.edu.cn/node/3587",
    ),
]


def main() -> None:
    config = yaml.safe_load(monitor.CONFIG.read_text(encoding="utf-8"))["settings"]
    now = datetime.now(ZoneInfo(config.get("timezone", "Asia/Shanghai")))
    found: dict[str, monitor.Notice] = {}

    for candidate in CASES:
        notice = monitor.build_notice(candidate, 2027, config, now)
        if notice is None:
            print(f"MISS {candidate.school}: {candidate.url}")
            continue
        found[candidate.school] = notice
        print(f"OK {notice.school}: {notice.title}")
        print(f"  published={notice.published} status={notice.status}")
        for value in notice.key_times:
            print(f"  time={value}")

    if len(found) != len(CASES):
        missing = sorted({item.school for item in CASES} - set(found))
        raise SystemExit(f"Known official notices were not parsed: {', '.join(missing)}")

    xmu = found["厦门大学"]
    if xmu.status != "已截止" or not any("7 月 7 日" in value for value in xmu.key_times):
        raise SystemExit("XMU application date range was not recognized as closed")
    if any("1-8 项" in value for value in xmu.key_times):
        raise SystemExit("A document item range was incorrectly recognized as a date")

    sysu = found["中山大学"]
    joined = "\n".join(sysu.key_times)
    for expected in ("8月12日", "8月20日", "8月27日"):
        if expected not in joined:
            raise SystemExit(f"SYSU key date missing: {expected}")

    print(f"validated={len(found)}/{len(CASES)}")


if __name__ == "__main__":
    main()

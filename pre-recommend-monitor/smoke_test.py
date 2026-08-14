#!/usr/bin/env python3
"""Regression check against known official 2027 pre-recommendation notices."""
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
    found = []
    for candidate in CASES:
        notice = monitor.build_notice(candidate, 2027, config, now)
        if notice is None:
            print(f"MISS {candidate.school}: {candidate.url}")
            continue
        found.append(notice)
        print(f"OK {notice.school}: {notice.title}")
        print(f"  published={notice.published} status={notice.status}")
        for value in notice.key_times:
            print(f"  time={value}")
    if not found:
        raise SystemExit("No known official 2027 notice could be parsed")
    print(f"validated={len(found)}/{len(CASES)}")


if __name__ == "__main__":
    main()

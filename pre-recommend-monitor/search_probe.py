#!/usr/bin/env python3
from __future__ import annotations

from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
QUERY = "site:sysu.edu.cn 2027 推免 计算机"

CASES = [
    ("bing-cn-html", "https://cn.bing.com/search", {"q": QUERY, "mkt": "zh-CN", "ensearch": "0", "count": "20"}),
    ("bing-cn-rss", "https://cn.bing.com/search", {"q": QUERY, "format": "rss", "mkt": "zh-CN", "ensearch": "0"}),
    ("baidu", "https://www.baidu.com/s", {"wd": QUERY, "rn": "20"}),
    ("sogou", "https://www.sogou.com/web", {"query": QUERY}),
    ("google", "https://www.google.com/search", {"q": QUERY, "hl": "zh-CN", "num": "20"}),
    ("ddg", "https://html.duckduckgo.com/html/", {"q": QUERY}),
]

s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})

for name, url, params in CASES:
    print("\n===", name, "===")
    try:
        r = s.get(url, params=params, timeout=20, allow_redirects=True)
        print("status", r.status_code, "url", r.url, "bytes", len(r.content), "type", r.headers.get("content-type"))
        text = r.text
        print("prefix", repr(text[:180]))
        soup = BeautifulSoup(text, "html.parser")
        print("title", repr(soup.title.get_text(" ", strip=True) if soup.title else ""))
        printed = 0
        selectors = [
            "li.b_algo h2 a",
            "div.result h3 a",
            "div.c-container h3 a",
            "div.vrwrap h3 a",
            "a.result__a",
            "a[href]",
        ]
        seen = set()
        for selector in selectors:
            for a in soup.select(selector):
                title = " ".join(a.get_text(" ", strip=True).split())
                href = a.get("href", "")
                key = (title, href)
                if key in seen or not title or not href:
                    continue
                seen.add(key)
                host = urlparse(href).hostname or ""
                print("result", repr(title[:140]), repr(href[:300]), "host", host)
                printed += 1
                if printed >= 8:
                    break
            if printed >= 8:
                break
    except Exception as exc:
        print("ERROR", type(exc).__name__, str(exc))

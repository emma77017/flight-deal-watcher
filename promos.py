"""Promo-announcement watcher: reads the deal blogs that historically broke every
qualifying China/Japan/Korea premium-cabin sale (piao.tips, pointstalent, etc.),
evaluates each post against OUR criteria (route + cabin + price), and only
surfaces the ones that fit. Non-qualifying matches are logged, never emailed."""

import json
import logging
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger(__name__)

USD_RE = [
    re.compile(r"\$\s?([\d,]{3,7})"),
    re.compile(r"([\d,]{3,7})\s*(?:美元|美金|USD)", re.I),
]
CNY_RE = [
    re.compile(r"(?:¥|￥|人民币|CNY|RMB)\s*([\d,]{4,7})", re.I),
    re.compile(r"([\d,]{4,7})\s*(?:人民币|元(?!旦))"),
]
WAN_RE = re.compile(r"([\d.]{1,4})\s*万")
W_SHORT_RE = re.compile(r"(\d)\s*[wW]\s*(\d)?")      # "1w8" = ¥18,000
RT_BARE_RE = re.compile(r"往返\s*\$?([\d,]{4})")      # "往返4300+" - USD context


def _prices_usd(text: str, cny_per_usd: float) -> list[int]:
    """Extract plausible per-person prices from a post, normalized to USD."""
    vals = []
    for rx in USD_RE:
        for m in rx.findall(text):
            v = int(m.replace(",", ""))
            if 800 <= v <= 20000:
                vals.append(v)
    for rx in CNY_RE:
        for m in rx.findall(text):
            v = int(m.replace(",", ""))
            if 9000 <= v <= 150000:
                vals.append(round(v / cny_per_usd))
    for m in WAN_RE.findall(text):
        try:
            v = float(m) * 10000
        except ValueError:
            continue
        if 9000 <= v <= 150000:
            vals.append(round(v / cny_per_usd))
    for m in W_SHORT_RE.findall(text):
        v = int(m[0]) * 10000 + (int(m[1]) * 1000 if m[1] else 0)
        if 9000 <= v <= 60000:
            vals.append(round(v / cny_per_usd))
    for m in RT_BARE_RE.findall(text):
        v = int(m.replace(",", ""))
        if 1500 <= v <= 9999:
            vals.append(v)
    return vals


# posts about the wrong direction (US -> China) are knowledge, not alerts
REVERSE_PATTERNS = ["美国直飞中国", "美国出发", "从美国", "美国回国", "us to china", "回国商务"]
FORWARD_PATTERNS = ["赴美", "飞美国", "到美国", "直飞美国", "上海", "杭州", "浦东", "东京", "成田",
                    "首尔", "仁川", "shanghai", "hangzhou", "tokyo", "seoul"]


def _is_reverse_direction(text: str) -> bool:
    return any(r in text for r in REVERSE_PATTERNS) and not any(f in text for f in FORWARD_PATTERNS)


def check_promos(cfg: dict, base: Path) -> tuple[list[dict], list[dict]]:
    """Returns (qualifying_hits, other_matches). Only qualifying_hits deserve an email."""
    p = cfg.get("promos", {})
    feeds = p.get("feeds", [])
    cabin_kw = [k.lower() for k in p.get("keywords_cabin", [])]
    origin_kw = [k.lower() for k in p.get("keywords_origin", [])]
    dest_kw = [k.lower() for k in p.get("keywords_dest", [])]
    max_usd = p.get("promo_max_usd") or cfg.get("deal", {}).get("max_price_per_person", 3800)
    cny_per_usd = p.get("cny_per_usd", 7.2)

    seen_path = base / "data" / "promos_seen.json"
    try:
        seen = set(json.loads(seen_path.read_text()))
    except Exception:
        seen = set()

    fits, others = [], []
    for url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=25).read()
            root = ET.fromstring(raw)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                desc = (item.findtext("description") or "")[:1500]
                guid = (item.findtext("guid") or link or title).strip()
                if not guid or guid in seen:
                    continue
                seen.add(guid)
                text = f"{title} {desc}".lower()
                if not (any(k in text for k in cabin_kw)
                        and any(k in text for k in origin_kw)
                        and any(k in text for k in dest_kw)):
                    continue
                prices = _prices_usd(text, cny_per_usd)
                hit = {"title": title, "link": link,
                       "best_price_usd": min(prices) if prices else None}
                if prices and min(prices) <= max_usd and not _is_reverse_direction(text):
                    fits.append(hit)
                else:
                    others.append(hit)
        except Exception as e:
            log.warning("promo feed %s failed: %s", url, e)

    seen_path.parent.mkdir(exist_ok=True)
    seen_path.write_text(json.dumps(sorted(seen)[-3000:]))
    return fits, others

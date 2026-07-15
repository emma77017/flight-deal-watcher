"""Promo-announcement watcher: reads the deal blogs that historically broke every
qualifying China/Japan/Korea premium-cabin sale (piao.tips, pointstalent, etc.)
and surfaces new posts matching our cabin + route keywords."""

import json
import logging
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger(__name__)


def check_promos(cfg: dict, base: Path) -> list[dict]:
    p = cfg.get("promos", {})
    feeds = p.get("feeds", [])
    cabin_kw = [k.lower() for k in p.get("keywords_cabin", [])]
    route_kw = [k.lower() for k in p.get("keywords_route", [])]

    seen_path = base / "data" / "promos_seen.json"
    try:
        seen = set(json.loads(seen_path.read_text()))
    except Exception:
        seen = set()

    hits = []
    for url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=25).read()
            root = ET.fromstring(raw)
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                link = (item.findtext("link") or "").strip()
                desc = (item.findtext("description") or "")[:800]
                guid = (item.findtext("guid") or link or title).strip()
                if not guid or guid in seen:
                    continue
                seen.add(guid)
                text = f"{title} {desc}".lower()
                if any(k in text for k in cabin_kw) and any(k in text for k in route_kw):
                    hits.append({"title": title.strip(), "link": link, "feed": url})
        except Exception as e:
            log.warning("promo feed %s failed: %s", url, e)

    seen_path.parent.mkdir(exist_ok=True)
    seen_path.write_text(json.dumps(sorted(seen)[-3000:]))
    return hits

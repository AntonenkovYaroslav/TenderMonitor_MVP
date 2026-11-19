import re
from .config import settings
def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё","е")).strip()
def classify(row):
    cfg = settings()
    title, desc = norm(row.get("title")), norm(row.get("description"))
    area = norm((row.get("region") or "") + " " + (row.get("address") or ""))
    text = " ".join([title, desc, area, norm(row.get("customer"))])
    found, group = [], "Не определено"
    for name, words in cfg["groups"].items():
        hits = [w for w in words if norm(w) in text]
        if hits and group == "Не определено": group = name
        found.extend(hits)
    region_ok = any(norm(x) in area for x in cfg["regions"])
    title_hits = sum(1 for w in found if norm(w) in title)
    score = min(100, title_hits * 22 + (len(found)-title_hits) * 9 + (20 if region_ok else -15))
    level = "Высокая" if score >= cfg["thresholds"]["high"] else ("Средняя" if score >= cfg["thresholds"]["medium"] else "Низкая")
    return {"category": group, "score": max(0, score), "relevance": level, "region_check": "Калининградская область" if region_ok else "Проверить регион", "keywords": ", ".join(dict.fromkeys(found))}

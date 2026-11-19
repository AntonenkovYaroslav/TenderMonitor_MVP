import re
from .config import settings

def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е")).strip()

# Окончания русских словоформ, длинные первыми. Стемминг без внешних зависимостей:
# одна и та же функция применяется к ключевым словам и к тексту закупки,
# поэтому сравниваются основы («ремонта дороги» совпадает с «ремонт дороги»).
_ENDINGS = sorted({
    "остью", "ости", "ость", "ами", "ями", "ого", "его", "ому", "ему",
    "ах", "ях", "ов", "ев", "ых", "их", "ый", "ий", "ая", "яя", "ое", "ее",
    "ые", "ие", "ую", "юю", "ой", "ей", "ем", "ом", "ам", "ям",
    "а", "я", "ы", "и", "е", "у", "ю", "о", "ь", "й",
}, key=len, reverse=True)
_CONSONANTS = "бвгджзклмнпрстфхцчшщ"
# Беглые гласные: песок/песка, щебень/щебня, каток/катка.
_FLEETING = re.compile(rf"([{_CONSONANTS}])[ое](к|нь|ль|нн|н)$")
# Отрицания и исключающий контекст рядом с совпадением: «не требуется поставка
# песка», «без песка», «за исключением песка» не должны давать баллов.
_NEGATIONS = {"не", "без", "кроме", "исключением", "отсутствует", "отсутствуют"}

def _stem(word):
    if len(word) < 4 or any(ch.isdigit() for ch in word):
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            word = word[: -len(ending)]
            break
    word = _FLEETING.sub(r"\1\2", word)
    # Адъективные суффиксы -н-/-нн-: «асфальтобетонная» -> основа как у «асфальтобетон».
    return re.sub(r"н+$", "", word)

def _tokens(text):
    return re.findall(r"[a-zа-я0-9]+", norm(text))

def _phrase_stems(phrase):
    return tuple(_stem(w) for w in _tokens(phrase))

def _matches(phrase_stems, stems, raw):
    """Позиции фразы по основам слов; совпадения рядом с отрицанием отбрасываются."""
    size = len(phrase_stems)
    if not size or size > len(stems):
        return []
    positions = []
    for i in range(len(stems) - size + 1):
        if tuple(stems[i:i + size]) != phrase_stems:
            continue
        before = raw[max(0, i - 3):i]
        after = raw[i + size:i + size + 2]
        if any(w in _NEGATIONS for w in before + after):
            continue
        positions.append(i)
    return positions

def classify(row):
    cfg = settings()
    title, desc = norm(row.get("title")), norm(row.get("description"))
    area = norm((row.get("region") or "") + " " + (row.get("address") or ""))
    text = " ".join([title, desc, area, norm(row.get("customer"))])
    raw = _tokens(text)
    stems = [_stem(w) for w in raw]
    title_raw = _tokens(title)
    title_stems = [_stem(w) for w in title_raw]
    area_raw = _tokens(area)
    area_stems = [_stem(w) for w in area_raw]
    found, group, best_hits = [], "Не определено", 0
    for name, words in cfg["groups"].items():
        hits = [phrase for phrase in words if _matches(_phrase_stems(phrase), stems, raw)]
        if hits:
            found.extend(hits)
            # Направление — группа с наибольшим числом совпавших фраз, а не первая попавшаяся.
            if len(hits) > best_hits:
                best_hits, group = len(hits), name
    region_ok = any(_matches(_phrase_stems(x), area_stems, area_raw) for x in cfg["regions"])
    title_hits = sum(1 for phrase in found if _matches(_phrase_stems(phrase), title_stems, title_raw))
    other_hits = len(found) - title_hits
    # Одно сильное совпадение в заголовке + регион = «Средняя»; два совпадения в
    # заголовке или комбинация заголовок+описание = «Высокая». Без региона балл
    # снижается, но не обнуляется: закупку всё равно видно в отчёте.
    score = min(100, title_hits * 35 + other_hits * 12 + (15 if region_ok else -10))
    thresholds = cfg["thresholds"]
    level = "Высокая" if score >= thresholds["high"] else ("Средняя" if score >= thresholds["medium"] else "Низкая")
    region_label = (cfg.get("region") or "").strip() or "Регион подтвержден"
    return {"category": group, "score": max(0, score), "relevance": level, "region_check": region_label if region_ok else "Проверить регион", "keywords": ", ".join(dict.fromkeys(found))}
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
# Список расширяется из конфига (ключ "negations").
_DEFAULT_NEGATIONS = {"не", "без", "кроме", "исключением", "отсутствует", "отсутствуют"}
# Веса зон и штрафов. Значения по умолчанию сохраняют прежнюю калибровку;
# переопределяются блоком "scoring" в config/keywords.json.
_DEFAULT_SCORING = {"title_weight": 35, "other_weight": 12, "region_bonus": 15,
                    "region_penalty": 10, "stop_penalty": 60}

def _stem(word):
    if len(word) < 4 or any(ch.isdigit() for ch in word):
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            word = word[: -len(ending)]
            break
    word = _FLEETING.sub(r"\1\2", word)
    # Отглагольные существительные на -ние: «оборудование»/«оборудования»,
    # «изыскание»/«изыскания» сводятся к одной основе (оборудова, изыска).
    word = re.sub(r"ни$", "", word)
    # Адъективные суффиксы -н-/-нн-: «асфальтобетонная» -> основа как у «асфальтобетон».
    return re.sub(r"н+$", "", word)

def _tokens(text):
    return re.findall(r"[a-zа-я0-9]+", norm(text))

def _phrase_stems(phrase):
    return tuple(_stem(w) for w in _tokens(phrase))

def _matches(phrase_stems, stems, raw, negations=_DEFAULT_NEGATIONS):
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
        if any(w in negations for w in before + after):
            continue
        positions.append(i)
    return positions

def _scoring(cfg):
    """Веса из конфига с безопасными значениями по умолчанию."""
    raw = cfg.get("scoring") or {}
    weights = dict(_DEFAULT_SCORING)
    for key in weights:
        try:
            weights[key] = int(raw.get(key, weights[key]))
        except (TypeError, ValueError):
            pass
    return weights

def _analyze(row, cfg):
    """Полный разбор оценки: совпавшие фразы по зонам, вклад региона, стоп-фразы, баллы.

    Общая основа для classify (результат для базы) и explain (пояснение
    в веб-панели): логика подсчёта существует в одном месте.
    """
    weights = _scoring(cfg)
    negations = _DEFAULT_NEGATIONS | {norm(w) for w in (cfg.get("negations") or []) if w}
    title, desc = norm(row.get("title")), norm(row.get("description"))
    area = norm((row.get("region") or "") + " " + (row.get("address") or ""))
    text = " ".join([title, desc, area, norm(row.get("customer"))])
    raw = _tokens(text)
    stems = [_stem(w) for w in raw]
    title_raw = _tokens(title)
    title_stems = [_stem(w) for w in title_raw]
    area_raw = _tokens(area)
    area_stems = [_stem(w) for w in area_raw]
    found, group, best_hits, best_spec = [], "Не определено", 0, 0
    for name, words in cfg["groups"].items():
        hits = [phrase for phrase in words if _matches(_phrase_stems(phrase), stems, raw, negations)]
        if hits:
            found.extend(hits)
            # Направление — группа с наибольшим числом совпавших фраз; при равенстве
            # побеждает более специфичная (длинная) фраза: «ремонт дороги» важнее «ремонт».
            spec = max(len(_tokens(phrase)) for phrase in hits)
            if len(hits) > best_hits or (len(hits) == best_hits and spec > best_spec):
                best_hits, best_spec, group = len(hits), spec, name
    # Одна и та же фраза может встретиться в нескольких группах — считаем её один раз.
    found = list(dict.fromkeys(found))
    region_ok = any(_matches(_phrase_stems(x), area_stems, area_raw, negations) for x in cfg["regions"])
    title_phrases = [phrase for phrase in found if _matches(_phrase_stems(phrase), title_stems, title_raw, negations)]
    other_phrases = [phrase for phrase in found if phrase not in title_phrases]
    # Стоп-фразы профиля (мебель, ИТ, ГСМ, питание и т.п.) в заголовке сильно
    # снижают балл: строительный шум («ремонт компьютеров», «монтаж серверов»)
    # не должен проходить как релевантный. Штраф плоский, за наличие стоп-фразы.
    stop_phrases = list(dict.fromkeys(
        phrase for phrase in (cfg.get("stop_phrases") or [])
        if _matches(_phrase_stems(phrase), title_stems, title_raw, negations)
    ))
    score = (len(title_phrases) * weights["title_weight"]
             + len(other_phrases) * weights["other_weight"]
             + (weights["region_bonus"] if region_ok else -weights["region_penalty"])
             - (weights["stop_penalty"] if stop_phrases else 0))
    score = min(100, score)
    thresholds = cfg["thresholds"]
    level = "Высокая" if score >= thresholds["high"] else ("Средняя" if score >= thresholds["medium"] else "Низкая")
    region_label = (cfg.get("region") or "").strip() or "Регион подтвержден"
    return {
        "category": group, "score": max(0, score), "raw_score": score,
        "relevance": level, "region_check": region_label if region_ok else "Проверить регион",
        "keywords": ", ".join(dict.fromkeys(found)),
        "title_phrases": title_phrases, "other_phrases": other_phrases, "region_ok": region_ok,
        "stop_phrases": stop_phrases, "weights": weights,
    }


def classify(row, cfg=None):
    """Классификация закупки.

    `cfg` — необязательный снимок конфигурации (по умолчанию читается
    config/keywords.json). Параметр добавлен для веб-панели: сбор с
    дополнительными ключевыми словами использует копию конфига, не меняя
    файл на диске. Контракт результата (5 ключей) сохраняется.
    """
    result = _analyze(row, cfg or settings())
    return {key: result[key] for key in ("category", "score", "relevance", "region_check", "keywords")}


def explain(row, cfg=None):
    """Разбор оценки для пояснения в панели: из чего сложился балл,
    какие пороги, веса и список регионов сейчас активны. Ничего не записывает."""
    cfg = cfg or settings()
    detail = _analyze(row, cfg)
    detail["thresholds"] = dict(cfg.get("thresholds") or {})
    detail["regions"] = list(cfg.get("regions") or [])
    detail["region_name"] = (cfg.get("region") or "").strip()
    detail["groups"] = list((cfg.get("groups") or {}).keys())
    return detail


def weights(cfg=None):
    """Активные веса зон и штрафов (для пояснений в панели)."""
    return _scoring(cfg or settings())
# -*- coding: utf-8 -*-
"""Строительный профиль: легитимные закупки проходят, шум отсекается стоп-фразами."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # корень проекта; работает после clone/ZIP
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from tendermonitor.classifier import classify, explain
from tendermonitor.config import settings

CFG = settings()
PASS = 0

def check(name, cond, detail=""):
    global PASS
    assert cond, f"FAIL: {name} {detail}"
    PASS += 1
    print(f"ok {PASS:02d} {name}")

def row(title, region="Калининградская область", description="", address=""):
    return {"title": title, "region": region, "description": description, "address": address, "customer": ""}

LEGIT = [
    ("Ремонт дороги по ул. Ленина", "Дороги и благоустройство"),
    ("Капитальный ремонт здания школы", "Ремонт и отделка"),
    ("Реконструкция здания школы", "Строительство и реконструкция"),
    ("Новое строительство школы", "Строительство и реконструкция"),
    ("Поставка песка и щебня", "Стройматериалы"),
    ("Монтаж системы отопления", "Инженерные сети и монтаж"),
    ("Строительство детского сада", "Строительство и реконструкция"),
    ("Асфальтирование дворовой территории", "Дороги и благоустройство"),
    ("Уборка снега с кровли", "Зимнее содержание дорог"),
    ("Строительство автомобильной дороги", "Строительство и реконструкция"),
    ("Строительство медицинского центра", "Строительство и реконструкция"),
    ("Монтаж системы кондиционирования", "Инженерные сети и монтаж"),
    ("Ремонт кровли многоквартирного дома", "Ремонт и отделка"),
    ("Инженерные изыскания для проектирования", "Проектирование и изыскания"),
]
for title, cat in LEGIT:
    c = classify(row(title), cfg=CFG)
    e = explain(row(title), cfg=CFG)
    check(f"legit {title[:30]}", c["category"] == cat and c["score"] >= 35 and not e["stop_phrases"],
          (c["category"], c["score"], e["stop_phrases"]))

NOISE = [
    "Ремонт компьютерной техники",
    "Поставка мебели для офиса",
    "Монтаж серверного оборудования",
    "Поставка кондиционеров",
    "Ремонт автомобилей",
    "Поставка продуктов питания",
    "Оказание услуг по уборке помещений",
    "Поставка бензина АИ-92",
    "Разработка программного обеспечения ЕГАИС",
    "Ремонт медицинского оборудования",
    "Поставка канцелярских товаров",
    "Оказание услуг по охране объектов",
    "Поставка лекарственных препаратов",
    "Ремонт оргтехники",
]
for title in NOISE:
    c = classify(row(title), cfg=CFG)
    e = explain(row(title), cfg=CFG)
    check(f"noise {title[:30]}", c["score"] == 0 and c["relevance"] == "Низкая" and e["stop_phrases"],
          (c["score"], c["relevance"], e["stop_phrases"]))

# контракт classify не изменился
c = classify(row("Ремонт дороги"), cfg=CFG)
check("classify keys", set(c) == {"category", "score", "relevance", "region_check", "keywords"}, set(c))
# отрицания по-прежнему работают
c = classify(row("Поставка песка не требуется"), cfg=CFG)
check("negation kept", c["keywords"] == "", c)
# веса из конфига
e = explain(row("Поставка песка"), cfg=CFG)
check("weights exposed", e["weights"]["title_weight"] == 35 and e["weights"]["stop_penalty"] == 60, e["weights"])

print(f"\nALL {PASS} PROFILE CHECKS PASSED")
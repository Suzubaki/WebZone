# domain.py - Единые доменные правила, нормализация дат, ПВГ, фаз лактации и валидация
import math
from datetime import date, datetime
from functools import lru_cache
from config import Config

# Классификация диагнозов по системам органов и патологиям (ветеринарный стандарт РБ)
PATHOLOGY_RULES = [
    ('Акушерство и гинекология', ['роды', 'моцерац', 'мацерац', 'эндометр', 'кист', 'яловост', 'маточн', 'сальпинг', 'матк', 'гинекол', 'фримартин']),
    ('Болезни вымени (маститы)', ['мастит', 'агалакти', 'вымен', 'соск']),
    ('Органы пищеварения', ['тимпани', 'гастро', 'язв', 'сычуг', 'рубец', 'перитонит', 'ретикул', 'атони', 'печен', 'цирроз', 'селезенк', 'преджелудк']),
    ('Органы дыхания', ['бронхопневмон', 'пневмон', 'легк', 'отек легких', 'дыхан']),
    ('Болезни молодняка (диспепсия)', ['диспепси', 'гипотрофи', 'колибакт', 'пупочн', 'эшерихиоз']),
    ('Конечности и травматизм', ['конечност', 'перелом', 'позвоноч', 'хромот', 'копыт', 'сустав', 'травм', 'мягких тканей', 'флегмон', 'рунхольц']),
    ('Нарушения обмена веществ', ['кетоз', 'ацидоз', 'обмен', 'парез']),
    ('Сердечно-сосудистая система', ['кровотеч', 'артери', 'сердц', 'инфаркт', 'миокард'])
]

PATHOLOGY_SYSTEM_NAMES = [r[0] for r in PATHOLOGY_RULES] + ['Прочие заболевания']

NON_COW_AGE_GROUPS = frozenset({
    'Телята (до 6 мес.)',
    'Ремонтный молодняк (6–18 мес.)',
    'Нетели (>18 мес.)',
    'Бычки на откорме',
    'Мертворождённые / Аборты'
})

MIN_VALID_DATE = date(1990, 1, 1)


def parse_iso_date(raw_val):
    """
    Парсит дату из datetime, date или строки ('YYYY-MM-DD', 'DD.MM.YYYY', 'YYYY/MM/DD')
    в объект datetime.date. Возвращает None при невалидном формате.
    """
    if raw_val is None:
        return None
    if isinstance(raw_val, datetime):
        return raw_val.date()
    if isinstance(raw_val, date):
        return raw_val
    txt = str(raw_val).strip()
    if not txt:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def normalize_date(raw_val):
    """Нормализует дату в канонический ISO формат 'YYYY-MM-DD' или возвращает None."""
    parsed = parse_iso_date(raw_val)
    return parsed.strftime('%Y-%m-%d') if parsed is not None else None


@lru_cache(maxsize=512)
def classify_pathology_system(reason):
    """Единый классификатор причины выбытия по системам органов."""
    r = (reason or '').lower()
    for sys_name, keywords in PATHOLOGY_RULES:
        for k in keywords:
            if k in r:
                return sys_name
    return 'Прочие заболевания'


@lru_cache(maxsize=512)
def normalize_age_group(raw_group, lactation=None):
    """Нормализует половозрастную группу (ПВГ) к каноническим значениям."""
    g = (raw_group or '').lower().strip()
    if any(k in g for k in ('мертворож', 'аборт', 'плод')):
        return 'Мертворождённые / Аборты'
    if any(k in g for k in ('телят', 'до 6', '0-6', '0–6', 'новорожд', 'молочн')):
        return 'Телята (до 6 мес.)'
    if any(k in g for k in ('быч', 'бык', 'откорм')):
        return 'Бычки на откорме'
    if any(k in g for k in ('нетел', 'старше 18', '> 18', '>18')):
        return 'Нетели (>18 мес.)'
    if 'сухосто' in g:
        return 'Сухостойные коровы'
    if any(k in g for k in ('новотел', '0-30', '0–30')):
        return 'Новотельные коровы (0–30 дн DIM)'
    if lactation == 1 or any(k in g for k in ('первотел', 'первотёл', '1-я лакт', '1-й лакт', '1 лакт')):
        return 'Коровы 1-й лактации (первотёлки)'
    if any(k in g for k in ('молодняк', '6-18', '6–18', 'от 6 до 18', '6-12', '6–12', 'ремонтн')) or (('тёлки' in g or 'телки' in g) and 'перво' not in g):
        return 'Ремонтный молодняк (6–18 мес.)'
    if (lactation and lactation > 1) or any(k in g for k in ('полновозраст', '2+ лакт')):
        return 'Полновозрастные коровы (2+ лакт.)'
    return 'Коровы дойного стада'


def compute_lactation_phase(age_group, lactation_days, lactation=None):
    """Единая точка расчёта фазы лактации по ПВГ и дням лактации (DIM)."""
    grp = (age_group or '').lower()
    if 'телят' in grp:
        return 'Молодняк (до 6 мес.)'
    if 'быч' in grp or 'откорм' in grp:
        return 'Бычки на откорме'
    if 'нетел' in grp:
        return 'Нетели'
    if 'сухосто' in grp:
        return 'Сухостой'
    if ('молодняк' in grp or 'ремонтн' in grp or (('тёлк' in grp or 'телк' in grp) and 'перво' not in grp)):
        return 'Молодняк (6–18 мес.)'

    dim = None
    if lactation_days is not None and str(lactation_days).strip() != '':
        try:
            dim = int(lactation_days)
        except (ValueError, TypeError):
            dim = None

    if dim is not None and dim >= 0:
        if dim <= 30:
            return 'Новотел (0–30 дн)'
        if dim <= 100:
            return 'Раздой (31–100 дн)'
        if dim <= 200:
            return 'Середина (101–200 дн)'
        return 'Спад (201–305+ дн)'

    if 'новотел' in grp:
        return 'Новотел (0–30 дн)'
    return 'Фаза не указана'


def _parse_bounded_int(raw, label, min_val, max_val, errors):
    if raw is None or str(raw).strip() == '':
        return None
    try:
        val = int(float(str(raw).strip().replace(',', '.')))
    except (ValueError, TypeError):
        errors.append(f'Поле «{label}» должно быть целым числом.')
        return None
    if val < min_val or val > max_val:
        errors.append(f'Поле «{label}» должно быть в диапазоне от {min_val} до {max_val}.')
        return None
    return val


def _parse_bounded_float(raw, label, min_val, max_val, errors):
    if raw is None or str(raw).strip() == '':
        return None
    try:
        val = float(str(raw).strip().replace('\u00a0', '').replace(' ', '').replace(',', '.'))
    except (ValueError, TypeError):
        errors.append(f'Поле «{label}» должно быть числом.')
        return None
    if not math.isfinite(val) or val < min_val or val > max_val:
        errors.append(f'Поле «{label}» должно быть в диапазоне от {min_val} до {max_val}.')
        return None
    return round(val, 2)


def validate_cow_payload(data, today_date=None):
    """
    Валидирует и нормализует данные карточки выбытия КРС.
    Возвращает (cleaned_dict, errors_list).
    """
    today_dt = today_date or date.today()
    errors = []

    category = (data.get('category') or '').strip().lower()
    reason = (data.get('reason') or '').strip()
    raw_date = data.get('disposal_date')

    if not category:
        errors.append('Выберите вид выбытия.')
    elif category not in Config.DISPOSAL_CATEGORIES:
        errors.append(f'Недопустимая категория выбытия: «{category}».')

    if not reason:
        errors.append('Укажите причину выбытия.')

    parsed_date = parse_iso_date(raw_date)
    if parsed_date is None:
        errors.append('Укажите корректную дату выбытия (ГГГГ-ММ-ДД).')
    elif parsed_date > today_dt:
        errors.append('Дата выбытия не может быть позже сегодняшнего дня.')
    elif parsed_date < MIN_VALID_DATE:
        errors.append('Дата выбытия не может быть раньше 1990 года.')

    lactation = _parse_bounded_int(data.get('lactation'), 'Номер лактации', 0, 20, errors)
    lactation_days = _parse_bounded_int(data.get('lactation_days'), 'Дни лактации (DIM)', 0, 1500, errors)
    weight = _parse_bounded_float(data.get('weight'), 'Живая масса (кг)', 1.0, 1500.0, errors)
    milk_yield = _parse_bounded_float(data.get('milk_yield'), 'Удой (кг)', 0.0, 30000.0, errors)
    book_value = _parse_bounded_float(data.get('book_value'), 'Балансовая стоимость (BYN)', 0.0, 100000.0, errors)
    gestation_days = _parse_bounded_int(data.get('gestation_days'), 'Срок стельности (дней)', 0, 320, errors)

    is_pregnant_val = 1 if data.get('is_pregnant') in (1, '1', True, 'true', 'on') else 0
    if gestation_days and gestation_days > 0:
        is_pregnant_val = 1
    if not is_pregnant_val:
        gestation_days = 0
    else:
        gestation_days = gestation_days or 0

    raw_ag = (data.get('age_group') or '').strip()
    age_group = raw_ag or 'Полновозрастные коровы (2+ лакт.)'
    if age_group not in Config.AGE_GROUPS:
        norm_ag = normalize_age_group(age_group, lactation)
        if norm_ag in Config.AGE_GROUPS:
            age_group = norm_ag

    ear_tag = (data.get('ear_tag') or '').strip()[:50] or None
    breed = (data.get('breed') or '').strip()[:80] or Config.DEFAULT_BREED
    notes = (data.get('notes') or '').strip()[:1000] or None

    lactation_phase = compute_lactation_phase(age_group, lactation_days, lactation)
    pathology_system = classify_pathology_system(reason)

    cleaned = {
        'category': category,
        'reason': reason[:255],
        'disposal_date': parsed_date.strftime('%Y-%m-%d') if parsed_date else '',
        'ear_tag': ear_tag,
        'lactation': lactation,
        'lactation_days': lactation_days,
        'lactation_phase': lactation_phase,
        'weight': weight,
        'notes': notes,
        'age_group': age_group,
        'breed': breed,
        'milk_yield': milk_yield,
        'book_value': book_value,
        'is_pregnant': is_pregnant_val,
        'gestation_days': gestation_days,
        'is_stillborn': 0,
        'pathology_system': pathology_system,
    }
    return cleaned, errors


def validate_stillborn_payload(data, today_date=None):
    """
    Валидирует и нормализует данные акта мертворождения / аборта.
    Возвращает (cleaned_dict, errors_list).
    """
    today_dt = today_date or date.today()
    errors = []

    raw_date = data.get('event_date') if data.get('event_date') is not None else data.get('disposal_date')
    if raw_date is None or str(raw_date).strip() == '':
        raw_date = today_dt
    parsed_date = parse_iso_date(raw_date)
    if parsed_date is None:
        errors.append('Укажите корректную дату события.')
    elif parsed_date > today_dt:
        errors.append('Дата события не может быть позже сегодняшнего дня.')
    elif parsed_date < MIN_VALID_DATE:
        errors.append('Дата события не может быть раньше 1990 года.')

    event_type = (data.get('event_type') or data.get('reason') or '').strip()
    if not event_type:
        errors.append('Укажите тип события / патологии плода.')

    cause = (data.get('cause') or '').strip()
    custom_cause = (data.get('custom_cause') or '').strip()
    if custom_cause and (cause == 'Другое' or not cause):
        cause = custom_cause
    if not cause:
        cause = event_type
    if not cause:
        errors.append('Укажите причину гибели плода.')

    def_gest, def_w = Config.infer_stillborn_defaults(event_type)

    raw_weight = data.get('fetus_weight') if data.get('fetus_weight') not in (None, '') else data.get('weight')
    if raw_weight not in (None, ''):
        fetus_weight = _parse_bounded_float(raw_weight, 'Масса плода (кг)', 0.1, 120.0, errors)
        if fetus_weight is None and not errors:
            fetus_weight = def_w
    else:
        fetus_weight = def_w

    raw_gest = data.get('gestation_days')
    if raw_gest not in (None, ''):
        gestation_days = _parse_bounded_int(raw_gest, 'Срок стельности (дней)', 0, 320, errors)
        if gestation_days is None and not errors:
            gestation_days = def_gest
    else:
        gestation_days = def_gest

    mother_tag = (data.get('mother_tag') if data.get('mother_tag') is not None else data.get('ear_tag') or '').strip()[:50] or None
    mother_group = (data.get('mother_group') or '').strip() or Config.DEFAULT_STILLBORN_MOTHER_GROUP
    fetus_sex = (data.get('fetus_sex') or '').strip() or 'Не определён'
    mother_condition = (data.get('mother_condition') or '').strip() or Config.MOTHER_CONDITIONS[0]
    attendant = (data.get('attendant') or '').strip()[:120] or Config.DEFAULT_ATTENDANT
    notes = (data.get('notes') or '').strip()[:1000] or None

    cleaned = {
        'event_date': parsed_date.strftime('%Y-%m-%d') if parsed_date else '',
        'mother_tag': mother_tag,
        'mother_group': mother_group,
        'event_type': event_type[:100],
        'fetus_sex': fetus_sex[:40],
        'fetus_weight': fetus_weight if fetus_weight is not None else def_w,
        'gestation_days': gestation_days if gestation_days is not None else def_gest,
        'cause': cause[:255],
        'mother_condition': mother_condition[:120],
        'attendant': attendant,
        'notes': notes,
    }
    return cleaned, errors

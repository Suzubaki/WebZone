# cow_queries.py - Построители SQL-фильтров и общие операции записи для журнала выбытия КРС
from domain import (
    PATHOLOGY_RULES,
    PATHOLOGY_SYSTEM_NAMES,
    classify_pathology_system,
    compute_lactation_phase,
    normalize_date,
)
from database import generate_cow_id, generate_stillborn_code


def _adult_lactating_sql():
    return (
        "LOWER(COALESCE(age_group, '')) NOT LIKE '%телят%' "
        "AND LOWER(COALESCE(age_group, '')) NOT LIKE '%молодняк%' "
        "AND LOWER(COALESCE(age_group, '')) NOT LIKE '%ремонтн%' "
        "AND LOWER(COALESCE(age_group, '')) NOT LIKE '%нетел%' "
        "AND LOWER(COALESCE(age_group, '')) NOT LIKE '%быч%' "
        "AND LOWER(COALESCE(age_group, '')) NOT LIKE '%сухосто%'"
    )


def build_age_group_sql(filter_val):
    if not filter_val or filter_val == 'all':
        return None, []
    f_lower = filter_val.lower().strip()
    if f_lower == 'молодняк_все':
        return (
            "(LOWER(COALESCE(age_group,'')) LIKE '%телят%' "
            "OR LOWER(COALESCE(age_group,'')) LIKE '%молодняк%' "
            "OR LOWER(COALESCE(age_group,'')) LIKE '%быч%' "
            "OR LOWER(COALESCE(age_group,'')) LIKE '%ремонтн%')"
        ), []
    if any(k in f_lower for k in ['телят', 'до 6', '0-6', '0–6', 'новорожд']):
        return (
            "(age_group = 'Телята (до 6 мес.)' OR LOWER(COALESCE(age_group,'')) LIKE '%телят%' "
            "OR LOWER(COALESCE(age_group,'')) LIKE '%до 6%')"
        ), []
    if any(k in f_lower for k in ['быч', 'бык', 'откорм']):
        return "(age_group = 'Бычки на откорме' OR LOWER(COALESCE(age_group,'')) LIKE '%быч%')", []
    if 'сухосто' in f_lower:
        return "(age_group = 'Сухостойные коровы' OR LOWER(COALESCE(age_group,'')) LIKE '%сухосто%')", []
    if any(k in f_lower for k in ['новотел', '0-30', '0–30']):
        return (
            f"((age_group = 'Новотельные коровы (0–30 дн DIM)' OR LOWER(COALESCE(age_group,'')) LIKE '%новотел%' "
            f"OR (lactation_days IS NOT NULL AND lactation_days BETWEEN 0 AND 30)) AND {_adult_lactating_sql()})"
        ), []
    if any(k in f_lower for k in ['первотел', 'первотёл', '1-я лакт', '1-й лакт', '1 лакт']):
        return (
            f"((age_group = 'Коровы 1-й лактации (первотёлки)' OR LOWER(COALESCE(age_group,'')) LIKE '%первот%' "
            f"OR lactation = 1) AND {_adult_lactating_sql()})"
        ), []
    if any(k in f_lower for k in ['полновозраст', '2+ лакт', '2+', '2 лакт', 'старше 2']):
        return (
            f"((age_group = 'Полновозрастные коровы (2+ лакт.)' OR LOWER(COALESCE(age_group,'')) LIKE '%полновозраст%' "
            f"OR lactation > 1 OR (LOWER(COALESCE(age_group,'')) LIKE '%дойное%' AND (lactation IS NULL OR lactation != 1))) "
            f"AND {_adult_lactating_sql()})"
        ), []
    if any(k in f_lower for k in ['нетел', '18']) and '6-18' not in f_lower and '6–18' not in f_lower:
        return "(age_group = 'Нетели (>18 мес.)' OR LOWER(COALESCE(age_group,'')) LIKE '%нетел%')", []
    if any(k in f_lower for k in ['молодняк', '6-18', '6–18', '6-12', '6–12', 'ремонтн', 'тёлк', 'телк']):
        return (
            "((age_group = 'Ремонтный молодняк (6–18 мес.)' OR LOWER(COALESCE(age_group,'')) LIKE '%молодняк%' "
            "OR LOWER(COALESCE(age_group,'')) LIKE '%ремонтн%') AND LOWER(COALESCE(age_group,'')) NOT LIKE '%телят%')"
        ), []
    return "(age_group = ? OR LOWER(COALESCE(age_group,'')) LIKE ?)", [filter_val, f"%{f_lower}%"]


def build_lactation_phase_sql(phase_val):
    if not phase_val or phase_val == 'all':
        return None, []
    p = phase_val.lower().strip()
    adult = _adult_lactating_sql()
    if ('ранн' in p and 'новотел' in p) or '0-15' in p or '0–15' in p:
        return f"(lactation_days IS NOT NULL AND lactation_days BETWEEN 0 AND 15 AND {adult})", []
    if ('поздн' in p and 'новотел' in p) or '16-30' in p or '16–30' in p:
        return f"(lactation_days IS NOT NULL AND lactation_days BETWEEN 16 AND 30 AND {adult})", []
    if 'новотел' in p:
        return f"((lactation_phase LIKE 'Новотел%' OR (lactation_days IS NOT NULL AND lactation_days BETWEEN 0 AND 30)) AND {adult})", []
    if 'раздой' in p or '31-100' in p or '31–100' in p:
        return f"((lactation_phase LIKE 'Раздой%' OR (lactation_days IS NOT NULL AND lactation_days BETWEEN 31 AND 100)) AND {adult})", []
    if 'середин' in p or '101-200' in p or '101–200' in p:
        return f"((lactation_phase LIKE 'Середина%' OR (lactation_days IS NOT NULL AND lactation_days BETWEEN 101 AND 200)) AND {adult})", []
    if 'спад' in p or '201' in p:
        return f"((lactation_phase LIKE 'Спад%' OR (lactation_days IS NOT NULL AND lactation_days > 200)) AND {adult})", []
    if 'сухосто' in p:
        return "(lactation_phase = 'Сухостой' OR age_group = 'Сухостойные коровы' OR LOWER(COALESCE(age_group,'')) LIKE '%сухосто%')", []
    if 'молодняк' in p:
        return "(lactation_phase LIKE 'Молодняк%' OR age_group IN ('Телята (до 6 мес.)', 'Ремонтный молодняк (6–18 мес.)', 'Бычки на откорме'))", []
    return "LOWER(COALESCE(lactation_phase,'')) LIKE ?", [f"%{p}%"]


def build_pathology_system_sql(system_name):
    """Фильтрация по нормализованному полю pathology_system (с поддержкой legacy-строк)."""
    if not system_name or system_name == 'all':
        return None, []
    s = system_name.strip()
    s_lower = s.lower()

    for canonical in PATHOLOGY_SYSTEM_NAMES:
        if canonical.lower() == s_lower:
            return "pathology_system = ?", [canonical]

    if 'ссз' in s_lower:
        return "pathology_system IN ('Сердечно-сосудистая система', 'Прочие заболевания')", []
    if 'проч' in s_lower:
        return "pathology_system = 'Прочие заболевания'", []

    # Fallback для произвольного текстового запроса
    for sys_name, keywords in PATHOLOGY_RULES:
        if s_lower in sys_name.lower():
            return "pathology_system = ?", [sys_name]
    return "LOWER(COALESCE(reason, '')) LIKE ?", [f"%{s_lower}%"]


def build_cows_where_clause(args, user_type, session_farm):
    """Единый построитель WHERE-условия для view_cows и api_cows."""
    farm_filter = (args.get('farm') or '').strip()
    category_filter = (args.get('category') or '').strip().lower()
    reason_filter = (args.get('reason') or '').strip()
    pathology_system_filter = (args.get('pathology_system') or '').strip()
    age_group_filter = (args.get('age_group') or '').strip()
    lactation_phase_filter = (args.get('lactation_phase') or '').strip()
    repro_filter = (args.get('repro_filter') or '').strip().lower()
    start_date = normalize_date(args.get('start_date')) or ''
    end_date = normalize_date(args.get('end_date')) or ''
    search_query = (args.get('search') or '').strip()[:100].lower()

    conditions = ["COALESCE(is_stillborn, 0) = 0"]
    params = []

    if user_type != 'admin':
        conditions.append("farm_name = ?")
        params.append(session_farm)
    elif farm_filter and farm_filter != 'all':
        conditions.append("farm_name = ?")
        params.append(farm_filter)

    if category_filter and category_filter != 'all':
        if category_filter in ('брак', 'выбраковка_сан', 'брак/убой'):
            conditions.append("category IN ('выбраковка', 'санитарный')")
        elif 'сан' in category_filter:
            conditions.append("category = 'санитарный'")
        else:
            conditions.append("category = ?")
            params.append(category_filter)

    if reason_filter and reason_filter != 'all':
        conditions.append("reason = ?")
        params.append(reason_filter)

    if pathology_system_filter and pathology_system_filter != 'all':
        ps_cond, ps_params = build_pathology_system_sql(pathology_system_filter)
        if ps_cond:
            conditions.append(ps_cond)
            params.extend(ps_params)

    if age_group_filter and age_group_filter != 'all':
        ag_cond, ag_params = build_age_group_sql(age_group_filter)
        if ag_cond:
            conditions.append(ag_cond)
            params.extend(ag_params)

    if lactation_phase_filter and lactation_phase_filter != 'all':
        lp_cond, lp_params = build_lactation_phase_sql(lactation_phase_filter)
        if lp_cond:
            conditions.append(lp_cond)
            params.extend(lp_params)

    if repro_filter == 'pregnant':
        conditions.append("(is_pregnant = 1 OR gestation_days > 0 OR LOWER(COALESCE(reason,'')) LIKE '%стельн%')")
    elif repro_filter == 'pregnant_late':
        conditions.append("(gestation_days >= 180 OR (is_pregnant = 1 AND LOWER(COALESCE(age_group,'')) LIKE '%сухосто%'))")

    if start_date and end_date:
        s_dt, e_dt = (start_date, end_date) if start_date <= end_date else (end_date, start_date)
        conditions.append("disposal_date BETWEEN ? AND ?")
        params.extend([s_dt, e_dt])
    elif start_date:
        conditions.append("disposal_date >= ?")
        params.append(start_date)
    elif end_date:
        conditions.append("disposal_date <= ?")
        params.append(end_date)

    if search_query:
        conditions.append(
            "(LOWER(COALESCE(cow_id,'')) LIKE ? OR LOWER(COALESCE(ear_tag,'')) LIKE ? "
            "OR LOWER(COALESCE(reason,'')) LIKE ? OR LOWER(COALESCE(farm_name,'')) LIKE ? "
            "OR LOWER(COALESCE(age_group,'')) LIKE ? OR LOWER(COALESCE(lactation_phase,'')) LIKE ?)"
        )
        wild = f"%{search_query}%"
        params.extend([wild, wild, wild, wild, wild, wild])

    where_sql = " WHERE " + " AND ".join(conditions)
    filter_meta = {
        'farm': farm_filter,
        'category': category_filter,
        'reason': reason_filter,
        'pathology_system': pathology_system_filter,
        'age_group': age_group_filter,
        'lactation_phase': lactation_phase_filter,
        'repro_filter': repro_filter,
        'start_date': start_date,
        'end_date': end_date,
        'period': (args.get('period') or '').strip(),
    }
    return where_sql, params, filter_meta


def insert_cow_record(cursor, farm_name, user_id, cleaned):
    """Единая точка вставки карточки выбытия КРС (используется в add, batch, import)."""
    cow_id = generate_cow_id(farm_name, conn=cursor.connection)
    lact_phase = cleaned.get('lactation_phase') or compute_lactation_phase(
        cleaned.get('age_group'), cleaned.get('lactation_days'), cleaned.get('lactation')
    )
    p_sys = cleaned.get('pathology_system') or classify_pathology_system(cleaned.get('reason'))
    cursor.execute('''
        INSERT INTO cows (
            cow_id, ear_tag, farm_name, category, reason, disposal_date,
            lactation, lactation_days, lactation_phase, pathology_system,
            weight, notes, age_group, breed, milk_yield, book_value,
            is_pregnant, gestation_days, is_stillborn, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
    ''', (
        cow_id, cleaned.get('ear_tag'), farm_name, cleaned['category'], cleaned['reason'],
        cleaned['disposal_date'], cleaned.get('lactation'), cleaned.get('lactation_days'),
        lact_phase, p_sys, cleaned.get('weight'), cleaned.get('notes'),
        cleaned.get('age_group'), cleaned.get('breed'), cleaned.get('milk_yield'),
        cleaned.get('book_value'), cleaned.get('is_pregnant', 0),
        cleaned.get('gestation_days', 0), user_id
    ))
    return cursor.lastrowid, cow_id


def insert_stillborn_record(cursor, farm_name, user_id, cleaned):
    """Единая точка вставки акта мертворождения / аборта."""
    sb_code = generate_stillborn_code(farm_name, conn=cursor.connection)
    cursor.execute('''
        INSERT INTO stillbirth_records (
            record_code, farm_name, event_date, mother_tag, mother_group,
            event_type, fetus_sex, fetus_weight, gestation_days, cause,
            mother_condition, attendant, notes, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        sb_code, farm_name, cleaned['event_date'], cleaned.get('mother_tag'),
        cleaned.get('mother_group'), cleaned['event_type'], cleaned.get('fetus_sex'),
        cleaned.get('fetus_weight'), cleaned.get('gestation_days', 0),
        cleaned['cause'], cleaned.get('mother_condition'),
        cleaned.get('attendant'), cleaned.get('notes'), user_id
    ))
    return cursor.lastrowid, sb_code

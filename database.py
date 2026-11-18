# database.py - Работа с базой данных SQLite
import logging
import os
import sqlite3
import threading
import time
from config import Config
from domain import classify_pathology_system, compute_lactation_phase

logger = logging.getLogger('cattle_app')
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s %(name)s: %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_indexes_checked = False
_wal_checked_for_db = None
_settings_cache = {}
_settings_cache_db = None
_settings_lock = threading.Lock()


def invalidate_settings_cache():
    global _settings_cache, _settings_cache_db
    with _settings_lock:
        _settings_cache = {}
        _settings_cache_db = None


def _create_indexes(cursor):
    """Единое определение оптимальных индексов без избыточного дублирования."""
    # Основные индексы таблицы cows (включая ожидаемые check_system.py)
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_farm_date ON cows (farm_name, disposal_date);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_category ON cows (category);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_cow_id ON cows (cow_id);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_ear_tag ON cows (ear_tag);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_disposal_date ON cows (disposal_date);')
    # Составные и аналитические индексы
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_date_id ON cows (disposal_date DESC, id DESC);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_date_cat_farm ON cows (disposal_date, category, farm_name);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cows_pathology_system ON cows (pathology_system);')
    # Индексы журнала аудита, причин и мертворождений
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs (created_at DESC);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_action_created ON audit_logs (action, created_at DESC);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_disposal_reasons_cat_active ON disposal_reasons (category, is_active, name);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stillbirth_farm_date ON stillbirth_records (farm_name, event_date);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stillbirth_event_type ON stillbirth_records (event_type);')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stillbirth_mother_tag ON stillbirth_records (mother_tag);')


def _drop_redundant_indexes(cursor):
    """Удаление избыточных старых индексов, дублирующих префиксы составных индексов."""
    redundant = (
        'idx_cows_farm_name',
        'idx_cows_reason',
        'idx_cows_cat_reason_farm',
        'idx_cows_farm_date_id',
        'idx_cows_category_date_id',
    )
    for idx_name in redundant:
        cursor.execute(f'DROP INDEX IF EXISTS {idx_name};')


def _ensure_indexes(conn):
    try:
        cursor = conn.cursor()
        _create_indexes(cursor)
        cursor.close()
    except Exception as exc:
        logger.debug("Не удалось проверить индексы при открытии соединения: %s", exc)


def _sqlite_lower(val):
    if val is None:
        return None
    if isinstance(val, str):
        return val.lower()
    return str(val).lower()


def get_db_connection():
    """Создает оптимизированное соединение с базой данных SQLite с поддержкой WAL-режима."""
    global _indexes_checked, _wal_checked_for_db
    conn = sqlite3.connect(Config.DATABASE, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.create_function("LOWER", 1, _sqlite_lower)

    cursor = conn.cursor()
    if _wal_checked_for_db != Config.DATABASE:
        cursor.execute('PRAGMA journal_mode=WAL;')
        _wal_checked_for_db = Config.DATABASE
        _indexes_checked = False
    cursor.execute('PRAGMA synchronous=NORMAL;')
    cursor.execute('PRAGMA temp_store=MEMORY;')
    cursor.execute('PRAGMA cache_size=-10000;')
    cursor.execute('PRAGMA busy_timeout=5000;')
    cursor.close()

    if not _indexes_checked:
        _indexes_checked = True
        _ensure_indexes(conn)

    return conn


def optimize_database_indexes():
    """Создание/обновление B-Tree индексов и удаление избыточных дубликатов."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        _drop_redundant_indexes(cursor)
        _create_indexes(cursor)
        conn.commit()
    except Exception as e:
        logger.warning("Ошибка при оптимизации индексов базы данных: %s", e)
    finally:
        if conn is not None:
            conn.close()


def sync_farm_data():
    """
    Синхронизация названий ферм в таблицах cows и stillbirth_records
    с актуальными названиями ферм пользователей (строго по created_by).
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE cows
            SET farm_name = (
                SELECT u.farm_name
                FROM users u
                WHERE u.id = cows.created_by
                  AND u.farm_name IS NOT NULL
                  AND u.farm_name != ''
            )
            WHERE created_by IN (
                SELECT id FROM users
                WHERE farm_name IS NOT NULL
                  AND farm_name != ''
            )
            AND COALESCE(farm_name, '') != (
                SELECT u.farm_name FROM users u WHERE u.id = cows.created_by
            )
        ''')
        cursor.execute('''
            UPDATE stillbirth_records
            SET farm_name = (
                SELECT u.farm_name
                FROM users u
                WHERE u.id = stillbirth_records.created_by
                  AND u.farm_name IS NOT NULL
                  AND u.farm_name != ''
            )
            WHERE created_by IN (
                SELECT id FROM users
                WHERE farm_name IS NOT NULL
                  AND farm_name != ''
            )
            AND COALESCE(farm_name, '') != (
                SELECT u.farm_name FROM users u WHERE u.id = stillbirth_records.created_by
            )
        ''')
        conn.commit()
    except Exception as e:
        logger.warning("Ошибка синхронизации названий ферм: %s", e)
    finally:
        if conn is not None:
            conn.close()


def _backfill_normalized_columns(cursor):
    """Заполняет pathology_system и нормализует lactation_phase для существующих записей."""
    cursor.execute('''
        SELECT id, reason, age_group, lactation_days, lactation, lactation_phase, pathology_system
        FROM cows
        WHERE pathology_system IS NULL OR pathology_system = ''
           OR lactation_phase IS NULL OR lactation_phase = ''
    ''')
    rows = cursor.fetchall()
    if not rows:
        return
    updates = []
    for r in rows:
        ps = r['pathology_system'] or classify_pathology_system(r['reason'])
        lp = r['lactation_phase'] or compute_lactation_phase(r['age_group'], r['lactation_days'], r['lactation'])
        updates.append((ps, lp, r['id']))
    cursor.executemany(
        'UPDATE cows SET pathology_system = ?, lactation_phase = ? WHERE id = ?',
        updates
    )


def init_db():
    """Инициализация базы данных и создание/миграция таблиц."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Таблица пользователей
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            user_type TEXT NOT NULL,
            farm_name TEXT,
            must_change_password INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(users)")
    user_cols = {row['name'] for row in cursor.fetchall()}
    if 'must_change_password' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0")

    # 2. Таблица коров (учет выбытия)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cow_id TEXT NOT NULL,
            ear_tag TEXT,
            farm_name TEXT NOT NULL,
            category TEXT NOT NULL,
            reason TEXT NOT NULL,
            disposal_date DATE NOT NULL,
            lactation INTEGER,
            weight REAL,
            notes TEXT,
            age_group TEXT,
            lactation_days INTEGER,
            lactation_phase TEXT,
            pathology_system TEXT,
            breed TEXT,
            milk_yield REAL,
            book_value REAL,
            is_pregnant INTEGER DEFAULT 0,
            gestation_days INTEGER DEFAULT 0,
            is_stillborn INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER,
            updated_at TIMESTAMP,
            updated_by INTEGER,
            FOREIGN KEY (created_by) REFERENCES users (id),
            FOREIGN KEY (updated_by) REFERENCES users (id)
        )
    ''')

    cursor.execute("PRAGMA table_info(cows)")
    existing_cols = {row['name'] for row in cursor.fetchall()}
    columns_to_add = {
        'ear_tag': 'TEXT',
        'lactation': 'INTEGER',
        'weight': 'REAL',
        'notes': 'TEXT',
        'age_group': 'TEXT',
        'lactation_days': 'INTEGER',
        'lactation_phase': 'TEXT',
        'pathology_system': 'TEXT',
        'breed': 'TEXT',
        'milk_yield': 'REAL',
        'book_value': 'REAL',
        'is_pregnant': 'INTEGER DEFAULT 0',
        'gestation_days': 'INTEGER DEFAULT 0',
        'is_stillborn': 'INTEGER DEFAULT 0',
        'updated_at': 'TIMESTAMP',
        'updated_by': 'INTEGER'
    }
    for col_name, col_type in columns_to_add.items():
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE cows ADD COLUMN {col_name} {col_type}")

    # 3. Динамический справочник причин выбытия
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS disposal_reasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(category, name)
        )
    ''')

    # 4. Журнал аудита действий (Audit Log)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT NOT NULL,
            farm_name TEXT,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            details TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 5. Системные настройки (зоотехнические нормативы, пороги и реквизиты)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            description TEXT
        )
    ''')

    # 6. Журнал учёта мертворождений и абортов
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stillbirth_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_code TEXT NOT NULL,
            farm_name TEXT NOT NULL,
            event_date DATE NOT NULL,
            mother_tag TEXT,
            mother_group TEXT,
            event_type TEXT NOT NULL,
            fetus_sex TEXT,
            fetus_weight REAL,
            gestation_days INTEGER DEFAULT 0,
            cause TEXT NOT NULL,
            mother_condition TEXT,
            attendant TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER,
            updated_at TIMESTAMP,
            updated_by INTEGER,
            FOREIGN KEY (created_by) REFERENCES users (id),
            FOREIGN KEY (updated_by) REFERENCES users (id)
        )
    ''')

    # 7. Таблица счётчиков для атомарной генерации номеров
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS farm_counters (
            farm_name TEXT PRIMARY KEY,
            last_cow_number INTEGER DEFAULT 0,
            last_stillborn_number INTEGER DEFAULT 0
        )
    ''')

    # Однократная миграция legacy-мертворождений из таблицы cows
    try:
        cursor.execute('''
            SELECT id, cow_id, ear_tag, farm_name, category, reason, disposal_date, weight, notes, age_group, gestation_days, created_at, created_by
            FROM cows
            WHERE is_stillborn = 1
               OR category = 'мертворождение'
               OR LOWER(age_group) LIKE '%мертворож%'
               OR LOWER(age_group) LIKE '%аборт%'
        ''')
        rows_to_migrate = cursor.fetchall()
        for r in rows_to_migrate:
            c_code = r['cow_id']
            if '-М' not in c_code:
                parts = c_code.split('-')
                c_code = f"{parts[0]}-М{parts[-1]}" if len(parts) > 1 else f"{c_code}-М"

            cursor.execute(
                'SELECT id FROM stillbirth_records WHERE record_code = ? OR (farm_name = ? AND event_date = ? AND cause = ?)',
                (c_code, r['farm_name'], r['disposal_date'], r['reason'])
            )
            if not cursor.fetchone():
                e_type = (
                    'Аборт (1-й триместр, до 90 дн.)'
                    if 'аборт' in (r['reason'] or '').lower() or 'аборт' in (r['age_group'] or '').lower()
                    else 'Мертворождённый телёнок (в срок)'
                )
                m_group = (
                    'Сухостойные коровы'
                    if 'сухосто' in (r['age_group'] or '').lower()
                    else Config.DEFAULT_STILLBORN_MOTHER_GROUP
                )
                cursor.execute('''
                    INSERT INTO stillbirth_records
                    (record_code, farm_name, event_date, mother_tag, mother_group, event_type, fetus_sex, fetus_weight, gestation_days, cause, mother_condition, attendant, notes, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    c_code, r['farm_name'], r['disposal_date'], r['ear_tag'], m_group,
                    e_type, 'Не определён', r['weight'] or Config.DEFAULT_STILLBORN_WEIGHT,
                    r['gestation_days'] or 0, r['reason'], 'Здорова, оставлена в стаде',
                    Config.DEFAULT_ATTENDANT, r['notes'] or 'Автоматически перенесено из общего журнала выбытия',
                    r['created_at'], r['created_by']
                ))

        if rows_to_migrate:
            migrated_ids = [r['id'] for r in rows_to_migrate]
            placeholders = ','.join('?' for _ in migrated_ids)
            cursor.execute(f"DELETE FROM cows WHERE id IN ({placeholders})", migrated_ids)
    except Exception as e:
        logger.warning("Ошибка миграции мертворождений: %s", e)

    # Заполнение нормализованных колонок для существующих записей
    _backfill_normalized_columns(cursor)

    # Заполнение справочника причин по умолчанию, если он пуст
    cursor.execute("SELECT COUNT(*) as cnt FROM disposal_reasons")
    if cursor.fetchone()['cnt'] == 0:
        for cat, reasons in Config.DISPOSAL_CATEGORIES.items():
            for reason in reasons:
                cursor.execute('''
                    INSERT OR IGNORE INTO disposal_reasons (category, name, is_active)
                    VALUES (?, ?, 1)
                ''', (cat, reason))

    # Заполнение настроек по умолчанию, если отсутствуют
    sc = Config.DEFAULT_SCORECARD_THRESHOLDS
    an = Config.DEFAULT_ANALYTICS_PARAMS
    default_settings = [
        ('norm_fresh_cow_disposal', str(sc['norm_fresh_cow_disposal']), 'Порог нормы: выбытие новотельных коров 0–30 DIM (%)'),
        ('warn_fresh_cow_disposal', str(sc['warn_fresh_cow_disposal']), 'Порог внимания: выбытие новотельных коров 0–30 DIM (%)'),
        ('crit_fresh_cow_disposal', str(sc['crit_fresh_cow_disposal']), 'Критический порог: выбытие новотельных коров 0–30 DIM (%)'),
        ('norm_first_lactation_culling', str(sc['norm_first_lactation_culling']), 'Порог нормы: выбраковка первотёлок (%)'),
        ('warn_first_lactation_culling', str(sc['warn_first_lactation_culling']), 'Порог внимания: выбраковка первотёлок (%)'),
        ('crit_first_lactation_culling', str(sc['crit_first_lactation_culling']), 'Критический порог: выбраковка первотёлок (%)'),
        ('norm_mortality_rate', str(sc['norm_mortality_rate']), 'Порог нормы: доля падежа в выбытии (%)'),
        ('warn_mortality_rate', str(sc['warn_mortality_rate']), 'Порог внимания: доля падежа в выбытии (%)'),
        ('crit_mortality_rate', str(sc['crit_mortality_rate']), 'Критический порог: доля падежа в выбытии (%)'),
        ('norm_locomotor_disorders', str(sc['norm_locomotor_disorders']), 'Порог нормы: заболевания копыт (%)'),
        ('warn_locomotor_disorders', str(sc['warn_locomotor_disorders']), 'Порог внимания: заболевания копыт (%)'),
        ('crit_locomotor_disorders', str(sc['crit_locomotor_disorders']), 'Критический порог: заболевания копыт (%)'),
        ('norm_mastitis_disorders', str(sc['norm_mastitis_disorders']), 'Порог нормы: заболевания вымени (%)'),
        ('warn_mastitis_disorders', str(sc['warn_mastitis_disorders']), 'Порог внимания: заболевания вымени (%)'),
        ('crit_mastitis_disorders', str(sc['crit_mastitis_disorders']), 'Критический порог: заболевания вымени (%)'),
        ('high_yield_threshold_kg', str(an['high_yield_threshold_kg']), 'Граница отнесения коровы к высокопродуктивным (кг/лакт.)'),
        ('matrix_warn_pct', str(an['matrix_warn_pct']), 'Порог внимания ячейки матрицы ферм (%)'),
        ('matrix_crit_pct', str(an['matrix_crit_pct']), 'Критический порог ячейки матрицы ферм (%)'),
        ('alert_death_growth_ratio', str(an['alert_death_growth_ratio']), 'Коэффициент вспышки падежа за 7 дн. к норме'),
        ('alert_cluster_min_count', str(an['alert_cluster_min_count']), 'Мин. число однотипных диагнозов за 30 дн. для тревоги'),
        ('farm_unp', Config.DEFAULT_UNP, 'УНП сельскохозяйственной организации (для ГИС AITS)'),
        ('farm_org_name', Config.DEFAULT_ORG_NAME, 'Полное наименование сельхозпредприятия (РБ)')
    ]
    for k, v, d in default_settings:
        cursor.execute('''
            INSERT OR IGNORE INTO system_settings (key, value, description)
            VALUES (?, ?, ?)
        ''', (k, v, d))

    conn.commit()
    conn.close()

    sync_farm_data()
    optimize_database_indexes()


def get_all_settings():
    """Получение всех настроек в виде словаря (с кэшированием в памяти процесса)."""
    global _settings_cache, _settings_cache_db
    with _settings_lock:
        if _settings_cache and _settings_cache_db == Config.DATABASE:
            return dict(_settings_cache)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT key, value FROM system_settings')
    rows = cursor.fetchall()
    conn.close()
    result = {r['key']: r['value'] for r in rows}

    with _settings_lock:
        _settings_cache = result
        _settings_cache_db = Config.DATABASE
        return dict(_settings_cache)


def get_setting(key, default=None):
    settings = get_all_settings()
    return settings.get(key, default)


def set_setting(key, value, description=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if description:
        cursor.execute('''
            INSERT INTO system_settings (key, value, description) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, description = excluded.description
        ''', (key, str(value), description))
    else:
        cursor.execute('''
            INSERT INTO system_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        ''', (key, str(value)))
    conn.commit()
    conn.close()
    invalidate_settings_cache()


def log_audit_event(username, farm_name, action, entity_type, entity_id=None, details=None, user_id=None, ip_address=None):
    """Запись события в журнал аудита с надёжным логированием сбоев."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO audit_logs (user_id, username, farm_name, action, entity_type, entity_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, username or 'System', farm_name, action, entity_type, str(entity_id) if entity_id else None, details, ip_address))
        conn.commit()
    except Exception:
        logger.exception("Ошибка записи события аудита action=%s entity=%s", action, entity_type)
    finally:
        if conn is not None:
            conn.close()


def _max_suffix_from_rows(rows, col_name, separator):
    max_num = 0
    for r in rows:
        code = r[col_name]
        if not code:
            continue
        try:
            tail = str(code).split(separator)[-1].replace('М', '')
            num = int(tail)
            if num > max_num:
                max_num = num
        except (IndexError, ValueError):
            continue
    return max_num


def _next_counter_value(cursor, farm_name, counter_col, table_name, code_col, prefix_sep):
    cursor.execute(f"SELECT {counter_col} FROM farm_counters WHERE farm_name = ?", (farm_name,))
    row = cursor.fetchone()
    if row is None:
        cursor.execute(f"SELECT {code_col} FROM {table_name} WHERE farm_name = ?", (farm_name,))
        current_max = _max_suffix_from_rows(cursor.fetchall(), code_col, '-')
        new_number = current_max + 1
        if counter_col == 'last_cow_number':
            cursor.execute(
                'INSERT INTO farm_counters (farm_name, last_cow_number, last_stillborn_number) VALUES (?, ?, 0)',
                (farm_name, new_number)
            )
        else:
            cursor.execute(
                'INSERT INTO farm_counters (farm_name, last_cow_number, last_stillborn_number) VALUES (?, 0, ?)',
                (farm_name, new_number)
            )
    else:
        new_number = (row[counter_col] or 0) + 1
        cursor.execute(
            f'UPDATE farm_counters SET {counter_col} = ? WHERE farm_name = ?',
            (new_number, farm_name)
        )
    return new_number


def generate_cow_id(farm_name, conn=None):
    """
    Атомарно генерирует следующий уникальный номер выбытия 'FarmName-XXX'
    с повторами при временной блокировке SQLite (без коллизионного timestamp fallback).
    """
    if conn is not None:
        cursor = conn.cursor()
        new_number = _next_counter_value(cursor, farm_name, 'last_cow_number', 'cows', 'cow_id', '-')
        return f"{farm_name}-{new_number:03d}"

    last_err = None
    for attempt in range(5):
        local_conn = get_db_connection()
        try:
            cursor = local_conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            new_number = _next_counter_value(cursor, farm_name, 'last_cow_number', 'cows', 'cow_id', '-')
            local_conn.commit()
            return f"{farm_name}-{new_number:03d}"
        except sqlite3.OperationalError as exc:
            local_conn.rollback()
            last_err = exc
            time.sleep(0.02 * (attempt + 1))
        except Exception as exc:
            local_conn.rollback()
            logger.exception("Критическая ошибка генерации cow_id для фермы %s", farm_name)
            raise RuntimeError(f"Не удалось сгенерировать уникальный номер выбытия: {exc}") from exc
        finally:
            local_conn.close()

    logger.error("Превышено число попыток генерации cow_id для %s: %s", farm_name, last_err)
    raise RuntimeError(f"База данных занята при генерации номера выбытия: {last_err}")


def get_reasons_for_category(category, include_inactive=False):
    """Получение причин выбытия из базы данных."""
    conn = get_db_connection()
    cursor = conn.cursor()
    if include_inactive:
        cursor.execute('SELECT * FROM disposal_reasons WHERE category = ? ORDER BY name ASC', (category,))
    else:
        cursor.execute('SELECT name FROM disposal_reasons WHERE category = ? AND is_active = 1 ORDER BY name ASC', (category,))
    results = cursor.fetchall()
    conn.close()

    if include_inactive:
        return [dict(r) for r in results]
    return [r['name'] for r in results]


def generate_stillborn_code(farm_name, conn=None):
    """
    Атомарно генерирует следующий уникальный номер акта мертворождения 'FarmName-МXXX'
    с повторами при блокировке SQLite (без коллизионного timestamp fallback).
    """
    if conn is not None:
        cursor = conn.cursor()
        new_number = _next_counter_value(cursor, farm_name, 'last_stillborn_number', 'stillbirth_records', 'record_code', '-')
        return f"{farm_name}-М{new_number:03d}"

    last_err = None
    for attempt in range(5):
        local_conn = get_db_connection()
        try:
            cursor = local_conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            new_number = _next_counter_value(cursor, farm_name, 'last_stillborn_number', 'stillbirth_records', 'record_code', '-')
            local_conn.commit()
            return f"{farm_name}-М{new_number:03d}"
        except sqlite3.OperationalError as exc:
            local_conn.rollback()
            last_err = exc
            time.sleep(0.02 * (attempt + 1))
        except Exception as exc:
            local_conn.rollback()
            logger.exception("Критическая ошибка генерации кода мертворождения для фермы %s", farm_name)
            raise RuntimeError(f"Не удалось сгенерировать номер акта мертворождения: {exc}") from exc
        finally:
            local_conn.close()

    logger.error("Превышено число попыток генерации кода мертворождения для %s: %s", farm_name, last_err)
    raise RuntimeError(f"База данных занята при генерации номера акта: {last_err}")

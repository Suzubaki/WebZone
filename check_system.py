# check_system.py - Скрипт автоматической самодиагностики системы
import os
import sys

def run_checks():
    print("=" * 60)
    print(" ДИАГНОСТИКА СИСТЕМЫ: ОАО «Новая Припять»")
    print("=" * 60)
    all_ok = True

    # 1. Проверка структуры Blueprints
    print("\n[1] Проверка модульной структуры (Flask Blueprints)...")
    expected_files = [
        'routes/__init__.py',
        'routes/main.py',
        'routes/auth.py',
        'routes/cows.py',
        'routes/reports.py',
        'routes/admin.py',
        'routes/api.py',
        'routes/stillborn.py',
        'extensions.py'
    ]
    for f in expected_files:
        if os.path.exists(f):
            print(f"  [OK] Файл найден: {f}")
        else:
            print(f"  [ОШИБКА] Не найден файл: {f}")
            all_ok = False

    # 2. Проверка базы данных, WAL-режима, внешних ключей и индексов
    print("\n[2] Проверка базы данных SQLite, WAL-режима и целостности...")
    try:
        from database import init_db, get_db_connection
        init_db()
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        if mode.lower() == 'wal':
            print(f"  [OK] Режим журнала SQLite: {mode.upper()} (параллельное чтение активно)")
        else:
            print(f"  [ОШИБКА] Текущий режим журнала: {mode} (ожидался WAL)")
            all_ok = False

        cursor.execute("PRAGMA foreign_keys;")
        fk_state = cursor.fetchone()[0]
        if fk_state == 1:
            print("  [OK] Контроль внешних ключей SQLite: ВКЛЮЧЕН (PRAGMA foreign_keys=ON)")
        else:
            print("  [ОШИБКА] Контроль внешних ключей SQLite отключен")
            all_ok = False

        cursor.execute("PRAGMA index_list(cows);")
        indexes = [row['name'] for row in cursor.fetchall()]
        expected_indexes = [
            'idx_cows_farm_date',
            'idx_cows_category',
            'idx_cows_cow_id',
            'idx_cows_ear_tag',
            'idx_cows_disposal_date'
        ]

        found_count = 0
        for idx in expected_indexes:
            if idx in indexes:
                print(f"  [OK] Индекс активен: {idx}")
                found_count += 1
            else:
                print(f"  [ОШИБКА] Индекс отсутствует: {idx}")
                all_ok = False

        if found_count == len(expected_indexes):
            print(f"  [OK] Все {found_count} ключевых индексов успешно оптимизируют запросы!")

        conn.close()
    except Exception as e:
        print(f"  [ОШИБКА БД]: {e}")
        all_ok = False

    # 3. Проверка регистрации и фактической работы маршрутов Flask
    print("\n[3] Проверка регистрации и отклика маршрутов приложения...")
    try:
        from app import app
        routes = [r.rule for r in app.url_map.iter_rules()]
        key_routes = [
            '/', '/login', '/logout', '/dashboard',
            '/cows', '/add_cow', '/import_cows',
            '/reports',
            '/reasons', '/audit_log', '/backup',
            '/img/logo.svg', '/img/logo.jpg'
        ]
        for kr in key_routes:
            if kr in routes or any(kr in r for r in routes):
                print(f"  [OK] Маршрут зарегистрирован: {kr}")
            else:
                print(f"  [ОШИБКА] Маршрут отсутствует: {kr}")
                all_ok = False

        with app.test_client() as client:
            checks = [
                ('GET /login', client.get('/login'), 200),
                ('GET /img/logo.svg', client.get('/img/logo.svg'), 200),
                ('GET /dashboard (без входа -> редирект)', client.get('/dashboard', follow_redirects=False), 302),
                ('GET /img/../../config.py (защита Path Traversal)', client.get('/img/../../config.py'), 404),
            ]
            for label, resp, expected_code in checks:
                if resp.status_code == expected_code:
                    print(f"  [OK] {label}: HTTP {resp.status_code}")
                else:
                    print(f"  [ОШИБКА] {label}: ожидался HTTP {expected_code}, получен {resp.status_code}")
                    all_ok = False
    except Exception as e:
        print(f"  [ОШИБКА ПРИЛОЖЕНИЯ]: {e}")
        all_ok = False

    # 4. Проверка официального логотипа
    print("\n[4] Проверка графических ассетов логотипа...")
    logo_paths = ['public/img/logo.svg', 'static/img/logo.svg', 'img/logo.svg']
    for lp in logo_paths:
        if os.path.exists(lp):
            size = os.path.getsize(lp)
            print(f"  [OK] Векторный логотип: {lp} ({size} байт)")
        else:
            print(f"  [ИНФО] {lp} будет отдаваться через встроенный генератор в app.py")

    print("\n" + "=" * 60)
    if all_ok:
        print(" ИТОГ: ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
        print(" Приложение полностью готово к запуску: python app.py")
    else:
        print(" ИТОГ: Обнаружены ошибки. Проверьте вывод выше.")
        sys.exit(1)
    print("=" * 60)

if __name__ == '__main__':
    run_checks()

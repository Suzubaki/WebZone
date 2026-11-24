# test_suite.py - Комплексный тест безопасности и функциональности системы ОАО «Новая Припять»
import io
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from config import Config
from database import get_db_connection, init_db, get_setting, reset_connection_state
from auth import register_user, update_user, authenticate_user
from charts import get_dashboard_full_data


class CattleSystemTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_db = Config.DATABASE
        cls._orig_env_db = os.environ.get('DATABASE_PATH')
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls._test_db_path = os.path.join(cls._tmp_dir.name, 'test_cattle.db')

        # Переключаем Config.DATABASE и окружение ДО импорта app, чтобы не затрагивать рабочую БД
        os.environ['DATABASE_PATH'] = cls._test_db_path
        Config.DATABASE = cls._test_db_path
        reset_connection_state()

        from app import app
        cls.app = app
        cls._orig_testing = app.config.get('TESTING')
        cls._orig_csrf = app.config.get('WTF_CSRF_ENABLED', True)

        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = True
        init_db()

        register_user('admin', 'AdminTest#123', 'admin', must_change_password=False)
        register_user('farm1', 'Farm1Test#123', 'farm', 'Ферма 1', must_change_password=False)
        register_user('farm2', 'Farm2Test#123', 'farm', 'Ферма 2', must_change_password=False)

        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.app.config['TESTING'] = cls._orig_testing
        cls.app.config['WTF_CSRF_ENABLED'] = cls._orig_csrf
        if cls._orig_env_db is None:
            os.environ.pop('DATABASE_PATH', None)
        else:
            os.environ['DATABASE_PATH'] = cls._orig_env_db
        Config.DATABASE = cls._orig_db
        reset_connection_state()
        cls._tmp_dir.cleanup()

    def setUp(self):
        from routes import auth as auth_routes
        from app import clear_all_rate_limits
        auth_routes.clear_all_auth_lockouts()
        clear_all_rate_limits()
        with self.client.session_transaction() as sess:
            sess.clear()

    def _extract_csrf(self, html_bytes):
        text = html_bytes.decode('utf-8', errors='ignore')
        m = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', text)
        if m:
            return m.group(1)
        m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', text)
        if m:
            return m.group(1)
        self.fail('CSRF-токен не найден на HTML-странице')

    def get_csrf_token(self, path='/login'):
        res = self.client.get(path, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        return self._extract_csrf(res.data)

    def login(self, username, password, remote_addr='127.0.0.1', follow_redirects=True):
        token = self.get_csrf_token('/login')
        return self.client.post(
            '/login',
            data={'username': username, 'password': password, 'csrf_token': token},
            environ_base={'REMOTE_ADDR': remote_addr},
            follow_redirects=follow_redirects
        )

    def logout(self):
        token = self.get_csrf_token('/dashboard')
        return self.client.post('/logout', data={'csrf_token': token}, follow_redirects=True)

    # 1. ТЕСТЫ АВТОРИЗАЦИИ И ДОСТУПА
    def test_01_public_and_auth_protection(self):
        for route in ['/dashboard', '/cows', '/settings', '/stillborn', '/reports']:
            res = self.client.get(route, follow_redirects=False)
            self.assertIn(res.status_code, [302, 401], f"Маршрут {route} должен быть защищен")

        res_fail = self.login('admin', 'wrong_pass')
        self.assertIn('Неверн'.encode('utf-8'), res_fail.data)

        res_ok = self.login('admin', 'AdminTest#123')
        self.assertEqual(res_ok.status_code, 200)
        self.assertIn(b'admin', res_ok.data)
        self.logout()

    # 2. ТЕСТЫ ДАШБОРДА И СКОРКАРДА
    def test_02_dashboard_and_scorecard(self):
        self.login('admin', 'AdminTest#123')
        res = self.client.get('/dashboard')
        self.assertEqual(res.status_code, 200)
        self.assertIn(b'dashboard', res.data.lower())

        data = get_dashboard_full_data(user_type='admin', farm_name='all', period='all')
        self.assertIn('kpi', data)
        self.assertIn('charts', data)
        self.assertIn('scorecard', data)

        sc_keys = [item['key'] for item in data['scorecard']]
        for ek in (
            'norm_fresh_cow_disposal',
            'norm_first_lactation_culling',
            'norm_mortality_rate',
            'norm_locomotor_disorders',
            'norm_mastitis_disorders',
        ):
            self.assertIn(ek, sc_keys)
        self.logout()

    # 3. ТЕСТЫ НАСТРОЕК И НОРМАТИВОВ (С РЕАЛЬНЫМ CSRF)
    def test_03_settings_and_thresholds(self):
        self.login('admin', 'AdminTest#123')
        self.assertEqual(self.client.get('/pricing').status_code, 404)

        csrf = self.get_csrf_token('/settings')

        test_payload = {
            'csrf_token': csrf,
            'norm_mortality_rate': '24.0',
            'matrix_warn_pct': '16.0',
            'high_yield_threshold_kg': '7000',
            'farm_org_name': 'ОАО «Новая Припять» Тест'
        }
        res = self.client.post('/settings', data=test_payload, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        self.assertAlmostEqual(float(get_setting('norm_mortality_rate')), 24.0)
        self.assertAlmostEqual(float(get_setting('matrix_warn_pct')), 16.0)
        self.assertAlmostEqual(float(get_setting('high_yield_threshold_kg')), 7000.0)
        self.assertEqual(get_setting('farm_org_name'), 'ОАО «Новая Припять» Тест')

        ajax_res = self.client.post(
            '/settings',
            json={'norm_mortality_rate': '25.0'},
            headers={'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': csrf}
        )
        self.assertEqual(ajax_res.status_code, 200)
        self.assertEqual(ajax_res.get_json().get('status'), 'success')
        self.logout()

    # 4. ТЕСТЫ CRUD ВЫБЫТИЯ СКОТА (С РЕАЛЬНЫМ CSRF)
    def test_04_cows_crud_lifecycle(self):
        self.login('farm1', 'Farm1Test#123')
        csrf = self.get_csrf_token('/add_cow')

        cow_payload = {
            'csrf_token': csrf,
            'ear_tag': 'BY999888777',
            'category': 'выбраковка',
            'reason': 'Мастит серозный',
            'disposal_date': datetime.now().strftime('%Y-%m-%d'),
            'lactation': '1',
            'lactation_days': '45',
            'lactation_phase': 'Раздой (31-100 дн)',
            'weight': '540',
            'age_group': 'Коровы 1-й лактации (первотёлки)',
            'milk_yield': '6500'
        }
        res_add = self.client.post('/add_cow', data=cow_payload, follow_redirects=True)
        self.assertEqual(res_add.status_code, 200)
        self.assertIn(b'BY999888777', res_add.data)
        self.logout()

        self.login('admin', 'AdminTest#123')
        res_search = self.client.get('/cows?search=BY999888777')
        self.assertEqual(res_search.status_code, 200)
        self.assertIn(b'BY999888777', res_search.data)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, pathology_system FROM cows WHERE ear_tag = 'BY999888777'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['pathology_system'], 'Болезни вымени (маститы)')
        cow_id = row['id']
        conn.close()

        csrf_cows = self.get_csrf_token('/cows')
        res_del = self.client.post(f'/delete_cow/{cow_id}', data={'csrf_token': csrf_cows}, follow_redirects=True)
        self.assertEqual(res_del.status_code, 200)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM cows WHERE ear_tag = 'BY999888777'")
        self.assertIsNone(cursor.fetchone())
        conn.close()
        self.logout()

    # 5. ТЕСТЫ ЖУРНАЛА МЕРТВОРОЖДЕНИЙ
    def test_05_stillbirth_module(self):
        self.login('farm1', 'Farm1Test#123')
        csrf = self.get_csrf_token('/add_stillborn')

        sb_payload = {
            'csrf_token': csrf,
            'event_date': datetime.now().strftime('%Y-%m-%d'),
            'mother_tag': 'BY777666555',
            'mother_group': 'Полновозрастные',
            'event_type': 'Мертворождение',
            'cause': 'Патология родов',
            'fetus_sex': 'Тёлочки',
            'fetus_weight': '28.5',
            'gestation_days': '278',
            'notes': 'Тестовая запись'
        }
        res_add = self.client.post('/add_stillborn', data=sb_payload, follow_redirects=True)
        self.assertEqual(res_add.status_code, 200)
        self.assertIn(b'BY777666555', res_add.data)
        html_sb_list = res_add.data.decode('utf-8')
        self.assertNotIn('Состояние матери', html_sb_list)
        self.assertNotIn('Принял отёл', html_sb_list)

        html_add_sb = self.client.get('/add_stillborn').data.decode('utf-8')
        self.assertNotIn('mother_condition', html_add_sb)
        self.assertNotIn('attendant', html_add_sb)

        html_add_cow = self.client.get('/add_cow').data.decode('utf-8')
        self.assertNotIn('sb_mother_condition', html_add_cow)
        self.assertNotIn('sb_attendant', html_add_cow)
        self.logout()

        self.login('admin', 'AdminTest#123')
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM stillbirth_records WHERE mother_tag = 'BY777666555'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        sb_id = row['id']
        conn.close()

        csrf_sb = self.get_csrf_token('/stillborn')
        res_del = self.client.post(f'/delete_stillborn/{sb_id}', data={'csrf_token': csrf_sb}, follow_redirects=True)
        self.assertEqual(res_del.status_code, 200)
        self.logout()

    # 6. ТЕСТЫ ОТЧЁТОВ И ЭКСПОРТА
    def test_06_reports_and_export(self):
        import openpyxl
        today_str = datetime.now().strftime('%Y-%m-%d')

        self.login('farm1', 'Farm1Test#123')
        csrf_add = self.get_csrf_token('/add_cow')
        self.client.post('/add_cow', data={
            'csrf_token': csrf_add,
            'form_mode': 'cow',
            'category': 'падёж',
            'reason': 'Диспепсия телят',
            'disposal_date': today_str,
            'ear_tag': 'BY_CALF_209',
            'age_group': 'Телята (до 6 мес.)',
            'lactation': '0',
            'weight': '45'
        }, follow_redirects=True)
        self.client.post('/add_cow', data={
            'csrf_token': csrf_add,
            'form_mode': 'cow',
            'category': 'выбраковка',
            'reason': 'Мастит хронический (атрофия долей)',
            'disposal_date': today_str,
            'ear_tag': 'BY_COW_210',
            'age_group': 'Полновозрастные коровы (2+ лакт.)',
            'lactation': '3',
            'weight': '520',
            'milk_yield': '6500'
        }, follow_redirects=True)
        self.logout()

        self.login('admin', 'AdminTest#123')
        csrf = self.get_csrf_token('/reports')

        res_209 = self.client.post('/generate_report', data={
            'csrf_token': csrf,
            'report_type': 'form_209_apk',
            'format': 'excel',
            'start_date': today_str,
            'end_date': today_str,
            'farm_filter': 'all'
        })
        self.assertEqual(res_209.status_code, 200)
        wb_209 = openpyxl.load_workbook(io.BytesIO(res_209.data))
        ws_209 = wb_209.active
        vals_209 = [ws_209.cell(row=r, column=7).value for r in range(10, ws_209.max_row + 1)]
        self.assertIn('Телята (до 6 мес.)', vals_209)
        self.assertIn('3 лакт.', vals_209)

        res_210 = self.client.post('/generate_report', data={
            'csrf_token': csrf,
            'report_type': 'form_210_apk',
            'format': 'excel',
            'start_date': today_str,
            'end_date': today_str,
            'farm_filter': 'all'
        })
        self.assertEqual(res_210.status_code, 200)
        self.assertEqual(res_210.content_type, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        wb_210 = openpyxl.load_workbook(io.BytesIO(res_210.data))
        ws_210 = wb_210.active
        vals_210 = [ws_210.cell(row=r, column=5).value for r in range(11, ws_210.max_row + 1)]
        self.assertIn('3 лакт.', vals_210)

        res_simple = self.client.post('/generate_report', data={
            'csrf_token': csrf,
            'report_type': 'simple',
            'format': 'excel',
            'start_date': today_str,
            'end_date': today_str,
            'farm_filter': 'all'
        })
        self.assertEqual(res_simple.status_code, 200)
        wb_simple = openpyxl.load_workbook(io.BytesIO(res_simple.data))
        ws_simple = wb_simple.active
        self.assertEqual(ws_simple.cell(row=1, column=7).value, 'Лактация / Возраст')
        vals_simple = [ws_simple.cell(row=r, column=7).value for r in range(2, ws_simple.max_row + 1)]
        self.assertIn('Телята (до 6 мес.)', vals_simple)

        res_svodka = self.client.post('/generate_original_excel', data={'csrf_token': csrf})
        self.assertEqual(res_svodka.status_code, 200)
        wb_svodka = openpyxl.load_workbook(io.BytesIO(res_svodka.data))
        ws_svodka = wb_svodka.active
        svodka_headers = [ws_svodka.cell(row=3, column=c).value for c in range(1, 6)]
        self.assertEqual(svodka_headers[4], 'Выбыло, всего (гол.)')

        res_sb_excel = self.client.get('/export_stillborn_excel')
        self.assertEqual(res_sb_excel.status_code, 200)
        self.assertEqual(res_sb_excel.content_type, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        conn = get_db_connection()
        conn.execute("DELETE FROM cows WHERE ear_tag IN ('BY_CALF_209', 'BY_COW_210')")
        conn.commit()
        conn.close()
        self.logout()

    # 7. ТЕСТЫ CSRF И АТОМАРНОСТИ ID
    def test_07_csrf_and_id_atomicity(self):
        from database import generate_cow_id, generate_stillborn_code

        id1 = generate_cow_id("МТФ-1")
        id2 = generate_cow_id("МТФ-1")
        self.assertTrue(id1.startswith("МТФ-1-"))
        self.assertNotEqual(id1, id2)

        sb_code = generate_stillborn_code("МТК-2")
        self.assertTrue(sb_code.startswith("МТК-2-М"))

        res_html_csrf = self.client.post('/login', data={'username': 'admin', 'password': '123'})
        self.assertEqual(res_html_csrf.status_code, 400)

        res_json_csrf = self.client.post('/settings', json={'norm_mortality_rate': '22.0'}, headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(res_json_csrf.status_code, 400)
        self.assertEqual(res_json_csrf.get_json().get('error'), 'csrf_error')

    # 8. ТЕСТЫ АУДИТА БЕЗОПАСНОСТИ И АРХИТЕКТУРЫ
    def test_08_security_and_domain_invariants(self):
        # 8.1 SECRET_KEY не захардкожен в коде
        self.assertTrue(self.app.secret_key)
        self.assertNotEqual(self.app.secret_key, 'novaya-pripyat-secret-key-2026')
        self.assertGreaterEqual(len(self.app.secret_key), 32)

        # 8.1b HTTPS-куки автоматически получают Secure (и SameSite=None в cross-site iframe)
        res_https = self.client.get('/login', headers={'X-Forwarded-Proto': 'https', 'Sec-Fetch-Site': 'cross-site'})
        cookies_https = ' '.join(res_https.headers.getlist('Set-Cookie'))
        self.assertIn('Secure', cookies_https)
        self.assertIn('SameSite=None', cookies_https)
        self.assertNotIn('X-XSS-Protection', res_https.headers)

        # 8.2 Блокировка брутфорса только по IP (без DoS по username)
        for _ in range(5):
            self.login('admin', 'wrong_password', remote_addr='10.10.0.1')
        res_locked = self.login('admin', 'AdminTest#123', remote_addr='10.10.0.1')
        self.assertEqual(res_locked.status_code, 429)

        # Другой IP спокойно входит под тем же логином admin
        res_other_ip = self.login('admin', 'AdminTest#123', remote_addr='10.10.0.2')
        self.assertEqual(res_other_ip.status_code, 200)
        self.assertIn(b'admin', res_other_ip.data)
        self.logout()

        # 8.3 Закрытая система: самостоятельная смена пароля отключена (/change_password -> 404)
        self.login('farm1', 'Farm1Test#123')
        res_dash = self.client.get('/dashboard')
        self.assertEqual(res_dash.status_code, 200)
        self.assertNotIn(b'/change_password', res_dash.data)
        res_cp = self.client.get('/change_password')
        self.assertEqual(res_cp.status_code, 404)
        self.logout()

        # 8.4 Переименование фермы пользователя A не затрагивает записи пользователя B
        register_user('shared_a', 'SharedA#123', 'farm', 'Ферма Общая')
        register_user('shared_b', 'SharedB#123', 'farm', 'Ферма Общая')
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username = 'shared_a'")
        id_a = cur.fetchone()['id']
        cur.execute("SELECT id FROM users WHERE username = 'shared_b'")
        id_b = cur.fetchone()['id']
        cur.execute(
            "INSERT INTO cows (cow_id, ear_tag, farm_name, category, reason, disposal_date, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('SHARE-1', 'BY000001', 'Ферма Общая', 'выбраковка', 'Мастит', '2026-03-01', id_a)
        )
        cur.execute(
            "INSERT INTO cows (cow_id, ear_tag, farm_name, category, reason, disposal_date, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('SHARE-2', 'BY000002', 'Ферма Общая', 'выбраковка', 'Мастит', '2026-03-01', id_b)
        )
        conn.commit()
        conn.close()

        ok, _ = update_user(id_a, 'shared_a', user_type='farm', farm_name='Ферма Переименованная')
        self.assertTrue(ok)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT farm_name FROM cows WHERE cow_id = 'SHARE-1'")
        self.assertEqual(cur.fetchone()['farm_name'], 'Ферма Переименованная')
        cur.execute("SELECT farm_name FROM cows WHERE cow_id = 'SHARE-2'")
        self.assertEqual(cur.fetchone()['farm_name'], 'Ферма Общая')
        conn.close()

        # 8.5 Валидация будущей даты и импорт без ложного срабатывания 'мертворож' в примечании
        self.login('farm1', 'Farm1Test#123')
        csrf_add = self.get_csrf_token('/add_cow')
        future_date = (datetime.now() + timedelta(days=10)).strftime('%Y-%m-%d')
        res_future = self.client.post('/add_cow', data={
            'csrf_token': csrf_add,
            'ear_tag': 'BY111222333',
            'category': 'выбраковка',
            'reason': 'Мастит',
            'disposal_date': future_date,
            'lactation': '1',
            'lactation_days': '10',
            'weight': '500',
            'age_group': 'Коровы 1-й лактации (первотёлки)',
            'milk_yield': '6000'
        }, follow_redirects=True)
        self.assertIn('позже сегодняшнего дня'.encode('utf-8'), res_future.data)

        csrf_imp = self.get_csrf_token('/import_cows')
        csv_content = (
            "Категория;Причина;Дата;Ушной №;Лактация;Масса;Примечание\n"
            "выбраковка;Мастит;2026-02-15;BY555444333;2;510;В истории болезни указано слово мертворождение без отношения к акту\n"
        ).encode('utf-8-sig')
        res_imp = self.client.post('/import_cows', data={
            'csrf_token': csrf_imp,
            'file': (io.BytesIO(csv_content), 'test_import.csv')
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(res_imp.status_code, 200)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM cows WHERE ear_tag = 'BY555444333'")
        self.assertIsNotNone(cur.fetchone(), "Обычная корова со словом 'мертворождение' в примечании должна попасть в cows")
        cur.execute("SELECT id FROM stillbirth_records WHERE mother_tag = 'BY555444333'")
        self.assertIsNone(cur.fetchone())
        conn.close()

        # 8.6 Проверка полного отсутствия экономической/финансовой части
        self.assertFalse(hasattr(Config, 'DEFAULT_PRICES'))
        res_add_get = self.client.get('/add_cow')
        self.assertEqual(res_add_get.status_code, 200)
        self.assertNotIn(b'book_value', res_add_get.data)
        self.assertNotIn(b'BYN', res_add_get.data)

        res_dash_clean = self.client.get('/dashboard')
        self.assertEqual(res_dash_clean.status_code, 200)
        self.assertNotIn(b'BYN', res_dash_clean.data)
        self.assertNotIn(b'economicsChart', res_dash_clean.data)

        self.logout()

        # 8.7 Проверка удаления тестовой кнопки/маршрута загрузки 1000 записей
        self.login('admin', 'AdminTest#123')
        res_admin_dash = self.client.get('/dashboard')
        self.assertEqual(res_admin_dash.status_code, 200)
        self.assertNotIn(b'seed_test_data', res_admin_dash.data)
        self.assertNotIn('Загрузить 1 000 коров'.encode('utf-8'), res_admin_dash.data)
        self.assertEqual(self.client.get('/seed_test_data').status_code, 404)
        self.logout()

    # 9. ПРОВЕРКА ПЕРВИЧНОГО СИДИНГА create_default_users И ОБЯЗАТЕЛЬНОЙ СМЕНЫ ПАРОЛЯ
    def test_09_initial_admin_only_on_empty_db(self):
        from create_users import create_default_users
        saved_db = Config.DATABASE
        saved_env = os.environ.get('INITIAL_ADMIN_PASSWORD')
        with tempfile.TemporaryDirectory() as td:
            db2 = os.path.join(td, 'empty_init.db')
            Config.DATABASE = db2
            reset_connection_state()
            os.environ['INITIAL_ADMIN_PASSWORD'] = 'InitEnvPass#999'
            try:
                init_db()
                create_default_users()
                u = authenticate_user('admin', 'InitEnvPass#999')
                self.assertIsNotNone(u)
                self.assertTrue(u.must_change_password)

                # При первом входе пользователь с must_change_password=True перенаправляется на смену пароля
                res_login = self.login('admin', 'InitEnvPass#999', follow_redirects=False)
                self.assertEqual(res_login.status_code, 302)
                self.assertIn('/first_login_password', res_login.headers['Location'])

                res_dash_blocked = self.client.get('/dashboard', follow_redirects=False)
                self.assertEqual(res_dash_blocked.status_code, 302)
                self.assertIn('/first_login_password', res_dash_blocked.headers['Location'])

                csrf_fl = self.get_csrf_token('/first_login_password')
                res_change = self.client.post('/first_login_password', data={
                    'csrf_token': csrf_fl,
                    'new_password': 'NewStrongPass#2026',
                    'confirm_password': 'NewStrongPass#2026',
                }, follow_redirects=True)
                self.assertEqual(res_change.status_code, 200)
                self.assertIsNotNone(authenticate_user('admin', 'NewStrongPass#2026'))
                self.assertFalse(authenticate_user('admin', 'NewStrongPass#2026').must_change_password)

                # Повторный запуск на непустой БД не должен менять или пересоздавать пользователей
                os.environ['INITIAL_ADMIN_PASSWORD'] = 'AnotherPass#888'
                create_default_users()
                self.assertIsNone(authenticate_user('admin', 'AnotherPass#888'))
            finally:
                if saved_env is None:
                    os.environ.pop('INITIAL_ADMIN_PASSWORD', None)
                else:
                    os.environ['INITIAL_ADMIN_PASSWORD'] = saved_env
                Config.DATABASE = saved_db
                reset_connection_state()

    # 10. СТРЕСС-ТЕСТ ОДНОВРЕМЕННОЙ ЗАПИСИ ИЗ НЕСКОЛЬКИХ ПОТОКОВ
    def test_10_concurrent_writes_no_lock_errors(self):
        from concurrent.futures import ThreadPoolExecutor
        from database import run_write_transaction, log_audit_event
        from cow_queries import insert_cow_record, insert_stillborn_record

        farms = ['Ферма Конк-1', 'Ферма Конк-2', 'Ферма Конк-3']
        today_str = datetime.now().strftime('%Y-%m-%d')

        def write_cow_worker(idx):
            farm = farms[idx % len(farms)]
            tag = f'BYCONC{idx:04d}'

            def _tx(conn, cursor):
                row_id, cow_id = insert_cow_record(cursor, farm, 1, {
                    'ear_tag': tag,
                    'category': 'выбраковка',
                    'reason': 'Мастит серозный',
                    'disposal_date': today_str,
                    'lactation': 2,
                    'lactation_days': 45,
                    'weight': 520.0,
                    'age_group': 'Полновозрастные коровы (2+ лакт.)',
                    'milk_yield': 6200.0,
                    'is_pregnant': 0,
                    'gestation_days': 0,
                })
                log_audit_event(
                    username='conc_worker',
                    farm_name=farm,
                    action='CONCURRENT_TEST_COW',
                    entity_type='COW',
                    entity_id=row_id,
                    details=f'Concurrent write {cow_id}',
                    user_id=1,
                    cursor=cursor
                )
                return cow_id

            return run_write_transaction(_tx)

        def write_sb_worker(idx):
            farm = farms[idx % len(farms)]
            tag = f'BYCONCSB{idx:04d}'

            def _tx(conn, cursor):
                row_id, sb_code = insert_stillborn_record(cursor, farm, 1, {
                    'event_date': today_str,
                    'mother_tag': tag,
                    'mother_group': 'Полновозрастные',
                    'event_type': 'Мертворождение',
                    'fetus_sex': 'Тёлочки',
                    'fetus_weight': 29.0,
                    'gestation_days': 276,
                    'cause': 'Патология родов',
                })
                log_audit_event(
                    username='conc_worker',
                    farm_name=farm,
                    action='CONCURRENT_TEST_SB',
                    entity_type='STILLBORN',
                    entity_id=row_id,
                    details=f'Concurrent write {sb_code}',
                    user_id=1,
                    cursor=cursor
                )
                return sb_code

            return run_write_transaction(_tx)

        cow_futures = []
        sb_futures = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            for i in range(24):
                cow_futures.append(pool.submit(write_cow_worker, i))
            for i in range(12):
                sb_futures.append(pool.submit(write_sb_worker, i))

        cow_ids = [f.result() for f in cow_futures]
        sb_codes = [f.result() for f in sb_futures]

        self.assertEqual(len(cow_ids), 24)
        self.assertEqual(len(set(cow_ids)), 24, "Сгенерированные cow_id должны быть строго уникальными при параллельной записи")
        self.assertEqual(len(sb_codes), 12)
        self.assertEqual(len(set(sb_codes)), 12, "Сгенерированные record_code должны быть строго уникальными при параллельной записи")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM cows WHERE ear_tag LIKE 'BYCONC%'")
        self.assertEqual(cur.fetchone()[0], 24)
        cur.execute("SELECT COUNT(*) FROM stillbirth_records WHERE mother_tag LIKE 'BYCONCSB%'")
        self.assertEqual(cur.fetchone()[0], 12)
        cur.execute("SELECT COUNT(*) FROM audit_logs WHERE action IN ('CONCURRENT_TEST_COW', 'CONCURRENT_TEST_SB')")
        self.assertEqual(cur.fetchone()[0], 36)
        conn.close()

    # 11. ТЕСТЫ СОХРАНЕНИЯ ПОЛЬЗОВАТЕЛЬСКИХ ПРИЧИН И РАСШИРЕНИЯ СПРАВОЧНИКА
    def test_11_reasons_preserved_and_expanded(self):
        from domain import classify_pathology_system
        from database import get_reasons_for_category

        # Классификация новых ветеринарных диагнозов
        self.assertEqual(classify_pathology_system('Смещение / заворот сычуга'), 'Органы пищеварения')
        self.assertEqual(classify_pathology_system('Сальмонеллёз телят'), 'Болезни молодняка (диспепсия)')
        self.assertEqual(classify_pathology_system('Болезнь Мортелларо (пальцевой дерматит)'), 'Конечности и травматизм')
        self.assertEqual(classify_pathology_system('Послеродовой парез (родильный парез)'), 'Нарушения обмена веществ')
        self.assertEqual(classify_pathology_system('Травматический перикардит'), 'Сердечно-сосудистая система')

        saved_db = Config.DATABASE
        with tempfile.TemporaryDirectory() as td:
            db_reasons = os.path.join(td, 'reasons_upgrade.db')
            Config.DATABASE = db_reasons
            reset_connection_state()
            try:
                # Имитация существующей БД старой версии с пользовательской причиной и скрытой причиной
                conn = sqlite3.connect(db_reasons)
                conn.execute('''
                    CREATE TABLE disposal_reasons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        category TEXT NOT NULL,
                        name TEXT NOT NULL,
                        is_active INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(category, name)
                    )
                ''')
                conn.execute("INSERT INTO disposal_reasons (category, name, is_active) VALUES ('падёж', 'Пользовательская причина фермы', 1)")
                conn.execute("INSERT INTO disposal_reasons (category, name, is_active) VALUES ('падёж', 'Патологические роды', 0)")
                conn.commit()
                conn.close()

                init_db()

                all_death = get_reasons_for_category('падёж', include_inactive=True)
                by_name = {r['name']: r['is_active'] for r in all_death}
                self.assertIn('Пользовательская причина фермы', by_name)
                self.assertEqual(by_name['Пользовательская причина фермы'], 1)
                self.assertEqual(by_name['Патологические роды'], 0)
                self.assertIn('Смещение / заворот сычуга', by_name)
                self.assertGreaterEqual(len(get_reasons_for_category('выбраковка')), 25)
                self.assertGreaterEqual(len(get_reasons_for_category('санитарный')), 20)

                # Удалённая вручную причина после обновления до v2 не должна возвращаться при повторном init_db()
                conn = sqlite3.connect(db_reasons)
                conn.execute("DELETE FROM disposal_reasons WHERE category = 'санитарный' AND name = 'Закупорка пищевода'")
                conn.commit()
                conn.close()

                init_db()
                san_reasons = get_reasons_for_category('санитарный')
                self.assertNotIn('Закупорка пищевода', san_reasons)
            finally:
                Config.DATABASE = saved_db
                reset_connection_state()

    # 12. ПРОВЕРКА ЖУРНАЛА БЕЗ ИКОНОК В ФАЗАХ/СТЕЛЬНОСТИ И ПРОСМОТРА ПО КОНКРЕТНЫМ ФЕРМАМ
    def test_12_clean_journal_and_per_farm_view(self):
        # Добавим по одной записи в две разные фермы для проверки раздельного просмотра
        self.login('farm1', 'Farm1Test#123')
        csrf1 = self.get_csrf_token('/add_cow')
        today_s = datetime.now().strftime('%Y-%m-%d')
        self.client.post('/add_cow', data={
            'csrf_token': csrf1,
            'ear_tag': 'BYFARM1TEST',
            'category': 'выбраковка',
            'reason': 'Мастит хронический (атрофия долей)',
            'disposal_date': today_s,
            'lactation': '2',
            'lactation_days': '45',
            'weight': '520',
            'age_group': 'Полновозрастные коровы (2+ лакт.)',
        }, follow_redirects=True)
        self.logout()

        self.login('farm2', 'Farm2Test#123')
        csrf2 = self.get_csrf_token('/add_cow')
        self.client.post('/add_cow', data={
            'csrf_token': csrf2,
            'ear_tag': 'BYFARM2TEST',
            'category': 'падёж',
            'reason': 'Гастроэнтерит',
            'disposal_date': today_s,
            'lactation': '1',
            'lactation_days': '15',
            'weight': '490',
            'age_group': 'Коровы 1-й лактации (первотёлки)',
        }, follow_redirects=True)
        self.logout()

        self.login('admin', 'AdminTest#123')

        res_cows = self.client.get('/cows')
        self.assertEqual(res_cows.status_code, 200)
        html_cows = res_cows.data.decode('utf-8')
        for unwanted in ('⚡', '📈', '⚖️', '📉', '💤', '👶', '⚠️', '🤰', 'bi-suit-heart'):
            self.assertNotIn(unwanted, html_cows)

        # Администратор видит все зарегистрированные фермы и быстрые кнопки переключения
        self.assertIn('Ферма 1', html_cows)
        self.assertIn('Ферма 2', html_cows)
        self.assertIn('data-farm="Ферма 1"', html_cows)
        self.assertIn('data-farm="Ферма 2"', html_cows)

        # Фильтрация в журнале по конкретной ферме
        res_cows_f1 = self.client.get('/cows?farm=Ферма 1')
        self.assertEqual(res_cows_f1.status_code, 200)
        html_cows_f1 = res_cows_f1.data.decode('utf-8')
        self.assertIn('BYFARM1TEST', html_cows_f1)
        self.assertNotIn('BYFARM2TEST', html_cows_f1)

        res_api_f2 = self.client.get('/api/cows?farm=Ферма 2')
        self.assertEqual(res_api_f2.status_code, 200)
        api_f2_tags = [c['ear_tag'] for c in res_api_f2.get_json()['cows']]
        self.assertIn('BYFARM2TEST', api_f2_tags)
        self.assertNotIn('BYFARM1TEST', api_f2_tags)

        res_sb = self.client.get('/stillborn')
        self.assertEqual(res_sb.status_code, 200)
        html_sb = res_sb.data.decode('utf-8')
        self.assertIn('Ферма 1', html_sb)
        self.assertIn('Ферма 2', html_sb)

        # На дашборде доступны как "Все фермы", так и быстрые кнопки конкретных ферм
        res_dash_all = self.client.get('/dashboard?period=all')
        self.assertEqual(res_dash_all.status_code, 200)
        html_dash_all = res_dash_all.data.decode('utf-8')
        self.assertIn('Все фермы', html_dash_all)
        self.assertIn('Ферма 1', html_dash_all)
        self.assertIn('Ферма 2', html_dash_all)

        res_dash = self.client.get('/dashboard?period=all&farm=' + 'Ферма 1')
        self.assertEqual(res_dash.status_code, 200)
        html_dash = res_dash.data.decode('utf-8')
        self.assertIn('Ферма / МТФ: <strong>Ферма 1</strong>', html_dash)
        self.assertIn('<input type="hidden" name="period" value="all">', html_dash)
        self.assertIn('BYFARM1TEST', html_dash)
        self.assertNotIn('BYFARM2TEST', html_dash)

        res_farms = self.client.get('/get_farms')
        self.assertEqual(res_farms.status_code, 200)
        farms_json = res_farms.get_json()
        self.assertIn('Ферма 1', farms_json)
        self.assertIn('Ферма 2', farms_json)

        self.logout()

    # 13. КОМПЛЕКСНАЯ ПРОВЕРКА ИСПРАВЛЕНИЙ АУДИТА БЕЗОПАСНОСТИ И НАДЁЖНОСТИ ДАННЫХ
    def test_13_audit_fixes_verification(self):
        from domain import validate_cow_payload
        from backup import create_db_backup_bytes, restore_db_from_upload
        from werkzeug.datastructures import FileStorage

        # 13.1 Защита от Path Traversal в маршрутах изображений и статики
        for bad_path in (
            '/img/../../instance/.secret_key',
            '/img/..%2f..%2fconfig.py',
            '/static/img/../../config.py',
            '/img/arbitrary.txt',
            '/assets/../../config.py',
        ):
            res_bad = self.client.get(bad_path)
            self.assertEqual(res_bad.status_code, 404, f"Ожидался 404 для {bad_path}")

        with self.client.get('/img/logo.svg') as res_svg:
            self.assertEqual(res_svg.status_code, 200)
        with self.client.get('/img/logo.png') as res_png:
            self.assertEqual(res_png.status_code, 200)

        # 13.2 Защита Logout от CSRF (GET запрещен -> 405, POST без CSRF -> 400)
        self.login('farm1', 'Farm1Test#123')
        self.assertEqual(self.client.get('/logout').status_code, 405)
        self.assertEqual(self.client.post('/logout').status_code, 400)

        # 13.3 Изоляция /get_farms для пользователей ферм
        res_f1_farms = self.client.get('/get_farms')
        self.assertEqual(res_f1_farms.status_code, 200)
        self.assertEqual(res_f1_farms.get_json(), ['Ферма 1'])

        # 13.4 Пакетное добавление не скрывает ошибочные строки и возвращает номера строк и причины
        csrf_batch = self.get_csrf_token('/add_multiple_cows')
        res_batch = self.client.post(
            '/add_multiple_cows',
            json=[
                {'category': 'выбраковка', 'reason': 'Мастит', 'ear_tag': 'BYBATCHOK1'},
                {'category': 'неверная_категория', 'reason': 'Ошибка', 'ear_tag': 'BYBATCHBAD1'},
            ],
            headers={'X-CSRFToken': csrf_batch, 'X-Requested-With': 'XMLHttpRequest'}
        )
        self.assertEqual(res_batch.status_code, 200)
        batch_json = res_batch.get_json()
        self.assertFalse(batch_json['success'])
        self.assertTrue(batch_json['partial'])
        self.assertEqual(batch_json['added_count'], 1)
        self.assertEqual(batch_json['skipped_count'], 1)
        self.assertEqual(batch_json['errors'][0]['row'], 2)

        # 13.5 Старый бинарный .xls отклоняется с понятным сообщением
        csrf_imp = self.get_csrf_token('/import_cows')
        res_xls = self.client.post('/import_cows', data={
            'csrf_token': csrf_imp,
            'file': (io.BytesIO(b'fake xls bytes'), 'old_report.xls')
        }, content_type='multipart/form-data', follow_redirects=True)
        self.assertEqual(res_xls.status_code, 200)
        self.assertIn('.xlsx'.encode('utf-8'), res_xls.data)
        self.logout()

        # 13.6 Единая политика паролей (>= 8 символов, буквы и цифры)
        ok_short, _ = register_user('weak_u', '123456', 'farm', 'Ферма 1')
        self.assertFalse(ok_short)
        ok_nodigit, _ = register_user('weak_u2', 'onlyletters', 'farm', 'Ферма 1')
        self.assertFalse(ok_nodigit)
        ok_good, _ = register_user('temp_del_u', 'ValidPass123', 'farm', 'Ферма 1')
        self.assertTrue(ok_good)

        # 13.7 PRAGMA foreign_keys=ON и ON DELETE SET NULL при удалении пользователя
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('PRAGMA foreign_keys;')
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT id FROM users WHERE username = 'temp_del_u'")
        del_uid = cur.fetchone()['id']
        cur.execute(
            "INSERT INTO cows (cow_id, ear_tag, farm_name, category, reason, disposal_date, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('FKTEST-1', 'BYFK0001', 'Ферма 1', 'выбраковка', 'Мастит', '2026-03-01', del_uid)
        )
        conn.commit()
        conn.close()

        self.login('admin', 'AdminTest#123')
        csrf_um = self.get_csrf_token('/user_management')
        res_del_u = self.client.post(f'/delete_user/{del_uid}', data={'csrf_token': csrf_um}, follow_redirects=True)
        self.assertEqual(res_del_u.status_code, 200)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, created_by FROM cows WHERE cow_id = 'FKTEST-1'")
        fk_row = cur.fetchone()
        self.assertIsNotNone(fk_row)
        self.assertIsNone(fk_row['created_by'])
        conn.close()
        self.logout()

        # 13.8 Валидация доменных полей: отклонение дробных целых и неизвестной ПВГ
        _, errs_frac = validate_cow_payload({
            'category': 'выбраковка',
            'reason': 'Мастит',
            'disposal_date': '2026-03-01',
            'lactation': '1.9',
            'age_group': 'Неизвестная группа 123'
        })
        self.assertTrue(any('целым числом' in e for e in errs_frac))
        self.assertTrue(any('половозрастная группа' in e for e in errs_frac))

        _, errs_int_float = validate_cow_payload({
            'category': 'выбраковка',
            'reason': 'Мастит',
            'disposal_date': '2026-03-01',
            'lactation': '2.0',
        })
        self.assertEqual(errs_int_float, [])

        # 13.9 Восстановление БД с использованием SQLite Online Backup API и откатом
        bak_bytes, _ = create_db_backup_bytes()
        bad_stream = FileStorage(stream=io.BytesIO(b'not a sqlite database' * 10), filename='bad.sqlite')
        ok_bad, _ = restore_db_from_upload(bad_stream, username='admin')
        self.assertFalse(ok_bad)

        good_stream = FileStorage(stream=io.BytesIO(bak_bytes), filename='good.sqlite')
        ok_good_restore, _ = restore_db_from_upload(good_stream, username='admin')
        self.assertTrue(ok_good_restore)

        # 13.10 Rate-limit и auth lockout полностью хранятся в SQLite (без словарей в памяти)
        from app import check_rate_limit, clear_all_rate_limits
        from routes import auth as auth_routes
        self.assertFalse(hasattr(auth_routes, '_failed_logins'))
        clear_all_rate_limits()

        # Проверяем запись блокировки входа в SQLite auth_lockouts
        for _ in range(5):
            self.login('admin', 'wrong_pass_sqlite', remote_addr='10.20.0.1')
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT fail_count, locked_until FROM auth_lockouts WHERE ip = '10.20.0.1'")
        lock_row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(lock_row)
        self.assertEqual(lock_row['fail_count'], 5)
        self.assertGreater(lock_row['locked_until'], 0)

        # Проверяем rate_limit в SQLite (20 burst-запросов разрешены, 21-й блокируется)
        for _ in range(20):
            allowed, _ = check_rate_limit('10.20.0.99', max_reqs=60, window_sec=60, burst_limit=20, burst_window=5, block_sec=30)
            self.assertTrue(allowed)
        allowed_21, wait_21 = check_rate_limit('10.20.0.99', max_reqs=60, window_sec=60, burst_limit=20, burst_window=5, block_sec=30)
        self.assertFalse(allowed_21)
        self.assertGreaterEqual(wait_21, 1)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT req_count, burst_count, blocked_until FROM rate_limits WHERE ip = '10.20.0.99'")
        rl_row = cur.fetchone()
        conn.close()
        self.assertIsNotNone(rl_row)
        self.assertEqual(rl_row['burst_count'], 21)
        self.assertGreater(rl_row['blocked_until'], 0)

        # Проверяем HTTP 429 + Retry-After через анти-DDoS guard в SQLite
        saved_rl_env = os.environ.get('ENABLE_TEST_RATELIMIT')
        os.environ['ENABLE_TEST_RATELIMIT'] = '1'
        try:
            csrf_rl = self.get_csrf_token('/login')
            res_429 = self.client.post(
                '/login',
                data={'username': 'admin', 'password': 'x', 'csrf_token': csrf_rl},
                environ_base={'REMOTE_ADDR': '10.20.0.99'}
            )
            self.assertEqual(res_429.status_code, 429)
            self.assertIn('Retry-After', res_429.headers)
        finally:
            if saved_rl_env is None:
                os.environ.pop('ENABLE_TEST_RATELIMIT', None)
            else:
                os.environ['ENABLE_TEST_RATELIMIT'] = saved_rl_env
            clear_all_rate_limits()

    # 14. МИГРАЦИЯ FK ON DELETE SET NULL НА УСТАРЕВШЕЙ БД С ДОП. КОЛОНКАМИ (autopsy_protocol)
    def test_14_legacy_fk_migration_preserves_extra_columns(self):
        saved_db = Config.DATABASE
        with tempfile.TemporaryDirectory() as td:
            legacy_db = os.path.join(td, 'legacy_extra_cols.db')
            Config.DATABASE = legacy_db
            reset_connection_state()
            try:
                conn = sqlite3.connect(legacy_db)
                conn.execute('''
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        user_type TEXT NOT NULL,
                        farm_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, user_type, farm_name) VALUES (1, 'legacy_u', 'hash', 'farm', 'Ферма 1')"
                )
                # Эмуляция старой таблицы cows с legacy-колонкой autopsy_protocol и FK без ON DELETE SET NULL
                conn.execute('''
                    CREATE TABLE cows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        cow_id TEXT NOT NULL,
                        farm_name TEXT NOT NULL,
                        category TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        disposal_date DATE NOT NULL,
                        autopsy_protocol TEXT DEFAULT 'протокол',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        created_by INTEGER,
                        FOREIGN KEY (created_by) REFERENCES users (id)
                    )
                ''')
                conn.execute(
                    "INSERT INTO cows (cow_id, farm_name, category, reason, disposal_date, autopsy_protocol, created_by) "
                    "VALUES ('LEG-1', 'Ферма 1', 'падёж', 'Мастит', '2026-01-10', 'Акт №5', 1)"
                )
                conn.execute(
                    "INSERT INTO cows (cow_id, farm_name, category, reason, disposal_date, autopsy_protocol, created_by) "
                    "VALUES ('LEG-2', 'Ферма 1', 'падёж', 'Кетоз', '2026-01-11', 'Акт №6', 999)"
                )
                conn.commit()
                conn.close()

                # Запуск init_db() должен без ошибок обновить FK и сохранить autopsy_protocol и строки
                init_db()

                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(cows)")
                col_names = {r['name'] for r in cur.fetchall()}
                self.assertIn('autopsy_protocol', col_names)

                cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='cows'")
                ddl = cur.fetchone()['sql'].upper()
                self.assertGreaterEqual(ddl.count('ON DELETE SET NULL'), 2)

                cur.execute("SELECT cow_id, autopsy_protocol, created_by FROM cows ORDER BY id")
                rows = cur.fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]['autopsy_protocol'], 'Акт №5')
                self.assertEqual(rows[0]['created_by'], 1)
                self.assertEqual(rows[1]['autopsy_protocol'], 'Акт №6')
                self.assertIsNone(rows[1]['created_by'])

                # Удаление пользователя должно выставить created_by = NULL без ошибки FK
                cur.execute("DELETE FROM users WHERE id = 1")
                conn.commit()
                cur.execute("SELECT created_by FROM cows WHERE cow_id = 'LEG-1'")
                self.assertIsNone(cur.fetchone()['created_by'])
                conn.close()
            finally:
                Config.DATABASE = saved_db
                reset_connection_state()

    # 15. ПРОВЕРКА HTTP-МАРШРУТОВ БЭКАПА И ВОССТАНОВЛЕНИЯ (В Т.Ч. RouteLimitRequest И WINDOWS-ОЧИСТКА)
    def test_15_backup_download_and_restore_routes(self):
        import backup as backup_mod
        from app import RouteLimitRequest

        self.assertTrue(RouteLimitRequest.max_content_length.fset is not None)

        self.login('admin', 'AdminTest#123')
        res_page = self.client.get('/backup')
        self.assertEqual(res_page.status_code, 200)

        res_rest_get = self.client.get('/backup/restore', follow_redirects=False)
        self.assertEqual(res_rest_get.status_code, 302)
        self.assertIn('/backup', res_rest_get.headers.get('Location', ''))

        res_dl = self.client.get('/backup/download')
        self.assertEqual(res_dl.status_code, 200)
        self.assertIn('attachment;', res_dl.headers.get('Content-Disposition', ''))
        self.assertTrue(res_dl.data.startswith(b'SQLite format 3'))

        # Восстановление через HTTP POST /backup/restore (проверка configure_route_upload_limits + RouteLimitRequest)
        csrf_bak = self.get_csrf_token('/backup')
        res_rest = self.client.post(
            '/backup/restore',
            data={
                'csrf_token': csrf_bak,
                'backup_file': (io.BytesIO(res_dl.data), 'backup.sqlite'),
            },
            content_type='multipart/form-data',
            follow_redirects=True,
        )
        self.assertEqual(res_rest.status_code, 200)
        self.assertIn('успешно восстановлена'.encode('utf-8'), res_rest.data)

        # Имитация WinError 32 при удалении временного файла в create_db_backup_bytes() не должна ронять скачивание
        orig_remove = backup_mod.os.remove
        def _failing_remove(_path):
            raise PermissionError('Simulated Windows file lock')

        backup_mod.os.remove = _failing_remove
        try:
            data_bytes, fname = backup_mod.create_db_backup_bytes()
            self.assertTrue(data_bytes.startswith(b'SQLite format 3'))
            self.assertTrue(fname.endswith('.sqlite'))
        finally:
            backup_mod.os.remove = orig_remove
            # Удаляем временные backup_*.sqlite из временной директории тестов (сама td удалится в tearDown)
            for leftover in os.listdir(os.path.dirname(Config.DATABASE)):
                if leftover.startswith('backup_') and leftover.endswith('.sqlite'):
                    try:
                        orig_remove(os.path.join(os.path.dirname(Config.DATABASE), leftover))
                    except OSError:
                        pass

        self.logout()

    # 16. ПРОВЕРКА ПЛАНОВОГО ЕЖЕНЕДЕЛЬНОГО И ЕЖЕМЕСЯЧНОГО БЭКАПА (scheduled_backup.py)
    def test_16_scheduled_weekly_and_monthly_backup(self):
        import inspect
        from scheduled_backup import run_scheduled_backup

        self.assertEqual(inspect.signature(run_scheduled_backup).parameters['keep'].default, 52)

        with tempfile.TemporaryDirectory() as bak_dir:
            # Создаем 2 искусственные старые копии за прошлые месяцы для проверки ротации (keep=2)
            for old_name in ('agro_backup_RB_20250101_030000.sqlite', 'agro_backup_RB_20250201_030000.sqlite'):
                with open(os.path.join(bak_dir, old_name), 'wb') as f:
                    f.write(b'SQLite format 3\x00' + b'\x00' * 100)

            # Еженедельный режим (по умолчанию)
            res1 = run_scheduled_backup(backup_dir=bak_dir, period='weekly', keep=2)
            self.assertTrue(res1['created'])
            self.assertTrue(os.path.exists(res1['path']))
            with open(res1['path'], 'rb') as f:
                self.assertTrue(f.read(15).startswith(b'SQLite format 3'))
            self.assertEqual(res1['removed'], ['agro_backup_RB_20250101_030000.sqlite'])

            # Повторный запуск на той же неделе не создаёт дубликат
            res2 = run_scheduled_backup(backup_dir=bak_dir, period='weekly', keep=2)
            self.assertFalse(res2['created'])
            self.assertEqual(res2['path'], res1['path'])

            # И в ежемесячном режиме при наличии копии за текущий месяц дубликат не создаётся
            res3 = run_scheduled_backup(backup_dir=bak_dir, period='monthly', keep=2)
            self.assertFalse(res3['created'])
            self.assertEqual(res3['path'], res1['path'])

            sqlite_files = [fn for fn in os.listdir(bak_dir) if fn.endswith('.sqlite')]
            self.assertEqual(len(sqlite_files), 2)

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT username, action, details FROM audit_logs WHERE username = 'AutoBackup' AND action = 'BACKUP'")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertIn('еженедельная', row['details'])
            conn.close()

        # Проверка встроенного автобэкапа внутри веб-приложения (без Tasks/cron) и скачивания из архива
        import scheduled_backup as sb_mod
        saved_base_dir = Config.BASE_DIR
        saved_env_ab = os.environ.get('ENABLE_AUTO_BACKUP_IN_TESTS')
        with tempfile.TemporaryDirectory() as web_bak_root:
            Config.BASE_DIR = web_bak_root
            sb_mod._last_check_by_target.clear()
            os.environ['ENABLE_AUTO_BACKUP_IN_TESTS'] = '1'
            try:
                self.login('admin', 'AdminTest#123')
                res_bak_page = self.client.get('/backup')
                self.assertEqual(res_bak_page.status_code, 200)
                html_bak = res_bak_page.data.decode('utf-8')
                self.assertIn('Автоматический еженедельный архив', html_bak)

                saved = sb_mod.list_saved_backups()
                self.assertEqual(len(saved), 1)
                fname = saved[0]['filename']
                self.assertIn(fname, html_bak)

                res_arch_dl = self.client.get(f'/backup/archive/{fname}')
                self.assertEqual(res_arch_dl.status_code, 200)
                self.assertTrue(res_arch_dl.data.startswith(b'SQLite format 3'))

                # Защита архивного скачивания от Path Traversal и посторонних имён
                self.assertEqual(self.client.get('/backup/archive/../../config.py').status_code, 404)
                self.assertEqual(self.client.get('/backup/archive/evil.sqlite').status_code, 404)
                self.logout()
            finally:
                Config.BASE_DIR = saved_base_dir
                sb_mod._last_check_by_target.clear()
                if saved_env_ab is None:
                    os.environ.pop('ENABLE_AUTO_BACKUP_IN_TESTS', None)
                else:
                    os.environ['ENABLE_AUTO_BACKUP_IN_TESTS'] = saved_env_ab


if __name__ == '__main__':
    unittest.main(verbosity=2)

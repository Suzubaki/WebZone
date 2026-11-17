# test_suite.py - Комплексный тест безопасности и функциональности системы ОАО «Новая Припять»
import io
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from config import Config
from database import get_db_connection, init_db, get_setting, invalidate_settings_cache
from auth import register_user, update_user, authenticate_user
from charts import get_dashboard_full_data


class CattleSystemTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._orig_db = Config.DATABASE
        cls._orig_testing = None
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls._test_db_path = os.path.join(cls._tmp_dir.name, 'test_cattle.db')

        from app import app
        cls.app = app
        cls._orig_testing = app.config.get('TESTING')
        cls._orig_csrf = app.config.get('WTF_CSRF_ENABLED', True)

        Config.DATABASE = cls._test_db_path
        invalidate_settings_cache()

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
        Config.DATABASE = cls._orig_db
        invalidate_settings_cache()
        cls._tmp_dir.cleanup()

    def setUp(self):
        from routes import auth as auth_routes
        auth_routes._failed_logins.clear()
        self.client.get('/logout', follow_redirects=False)

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
        return self.client.get('/logout', follow_redirects=True)

    # 1. ТЕСТЫ АВТОРИЗАЦИИ И ДОСТУПА
    def test_01_public_and_auth_protection(self):
        for route in ['/dashboard', '/cows', '/pricing', '/stillborn', '/reports']:
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

    # 3. ТЕСТЫ НАСТРОЕК PRICING (С РЕАЛЬНЫМ CSRF)
    def test_03_pricing_and_settings(self):
        self.login('admin', 'AdminTest#123')
        csrf = self.get_csrf_token('/pricing')

        test_payload = {
            'csrf_token': csrf,
            'meat_price_per_kg': '6.80',
            'milk_price_per_kg': '1.25',
            'replacement_cost': '3050.0',
            'price_calves': '950.0',
            'norm_mortality_rate': '24.0'
        }
        res = self.client.post('/pricing', data=test_payload, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        self.assertAlmostEqual(float(get_setting('meat_price_per_kg')), 6.80)
        self.assertAlmostEqual(float(get_setting('milk_price_per_kg')), 1.25)
        self.assertAlmostEqual(float(get_setting('replacement_cost')), 3050.0)

        ajax_res = self.client.post(
            '/pricing',
            json={'meat_price_per_kg': '6.50'},
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
            'milk_yield': '6500',
            'book_value': '3200'
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
        self.login('admin', 'AdminTest#123')
        csrf = self.get_csrf_token('/reports')

        today_str = datetime.now().strftime('%Y-%m-%d')
        res_210 = self.client.post('/generate_report', data={
            'csrf_token': csrf,
            'report_type': 'form_210_apk',
            'report_format': 'excel',
            'start_date': '2026-01-01',
            'end_date': today_str,
            'farm_filter': 'all'
        })
        self.assertEqual(res_210.status_code, 200)
        self.assertEqual(res_210.content_type, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

        res_sb_excel = self.client.get('/export_stillborn_excel')
        self.assertEqual(res_sb_excel.status_code, 200)
        self.assertEqual(res_sb_excel.content_type, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
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

        res_json_csrf = self.client.post('/pricing', json={'meat_price_per_kg': '7.0'}, headers={'X-Requested-With': 'XMLHttpRequest'})
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
            'milk_yield': '6000',
            'book_value': '3000'
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
        self.logout()

    # 9. ПРОВЕРКА ПЕРВИЧНОГО СИДИНГА create_default_users ТОЛЬКО ПРИ ПУСТОЙ БД
    def test_09_initial_admin_only_on_empty_db(self):
        from create_users import create_default_users
        saved_db = Config.DATABASE
        saved_env = os.environ.get('INITIAL_ADMIN_PASSWORD')
        with tempfile.TemporaryDirectory() as td:
            db2 = os.path.join(td, 'empty_init.db')
            Config.DATABASE = db2
            invalidate_settings_cache()
            os.environ['INITIAL_ADMIN_PASSWORD'] = 'InitEnvPass#999'
            try:
                init_db()
                create_default_users()
                u = authenticate_user('admin', 'InitEnvPass#999')
                self.assertIsNotNone(u)
                self.assertFalse(u.must_change_password)

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
                invalidate_settings_cache()


if __name__ == '__main__':
    unittest.main(verbosity=2)

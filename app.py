# app.py - Главная точка входа и фабрика приложения Flask
import gzip
import logging
import os
import threading
import time
from datetime import datetime
from flask import Flask, Request as FlaskRequest, abort, session, send_file, send_from_directory, Response, url_for, request, jsonify, redirect, flash
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from database import init_db, get_all_settings, run_write_transaction
from auth import get_user_by_id
from extensions import csrf

from routes import (
    main_bp,
    auth_bp,
    cows_bp,
    reports_bp,
    admin_bp,
    api_bp,
    stillborn_bp
)

logger = logging.getLogger('cattle_app')

class DynamicCookieSessionInterface(SecureCookieSessionInterface):
    """Динамически включает Secure (и SameSite=None для HTTPS iframe) при наличии HTTPS."""

    def get_cookie_secure(self, flask_app):
        if flask_app.config.get('SESSION_COOKIE_SECURE'):
            return True
        return bool(request.is_secure)

    def get_cookie_samesite(self, flask_app):
        if request.is_secure and request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return 'None'
        return super().get_cookie_samesite(flask_app)


class RouteLimitRequest(FlaskRequest):
    """Поддержка индивидуального max_content_length на маршрут (совместимо с Flask 2.x / 3.0.x / 3.1+)."""
    _custom_max_content_length = None

    @property
    def max_content_length(self):
        if self._custom_max_content_length is not None:
            return self._custom_max_content_length
        return super().max_content_length

    @max_content_length.setter
    def max_content_length(self, value):
        self._custom_max_content_length = value


app = Flask(__name__)
app.request_class = RouteLimitRequest
app.config.from_object(Config)
app.session_interface = DynamicCookieSessionInterface()

# Корректная обработка X-Forwarded-* заголовков за reverse proxy (PythonAnywhere / Nginx)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Регистрируем лимит размера для восстановления БД ДО csrf.init_app(app),
# чтобы CSRF-парсер формы не отклонял бэкапы > 16 МБ (лимит 50 МБ только для /backup/restore).
@app.before_request
def configure_route_upload_limits():
    if request.path.rstrip('/') == '/backup/restore' and request.method == 'POST':
        request.max_content_length = app.config.get('BACKUP_MAX_CONTENT_LENGTH', 50 * 1024 * 1024)

# Включаем CSRF-защиту Flask-WTF (управляется Config.WTF_CSRF_ENABLED = True)
csrf.init_app(app)

_app_init_lock = threading.Lock()
_initialized_db_path = None


def ensure_app_initialized():
    """
    Ленивая и строго проверяемая инициализация БД для текущего Config.DATABASE.
    Не выполняется при импорте модуля (чтобы тесты не затрагивали рабочую БД)
    и не скрывает ошибки инициализации.
    """
    global _initialized_db_path
    if _initialized_db_path == Config.DATABASE:
        return
    with _app_init_lock:
        if _initialized_db_path == Config.DATABASE:
            return
        init_db()
        from create_users import create_default_users
        create_default_users()
        _initialized_db_path = Config.DATABASE

_FALLBACK_LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><circle cx="32" cy="32" r="30" fill="#1b5e20"/><text x="32" y="39" font-family="sans-serif" font-size="22" font-weight="bold" fill="#ffd54f" text-anchor="middle">НП</text></svg>'
NEW_LOGO_REL = os.path.join('public', 'assets', 'aistudio', '__________________________________f05f89ea.png')


def _get_new_logo_path():
    candidates = [
        os.path.join(app.root_path, NEW_LOGO_REL),
        os.path.join(app.root_path, 'static', 'img', 'logo.png'),
        os.path.join(app.root_path, 'public', 'img', 'logo.png'),
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.isfile(c):
            return c
    return None


_ALLOWED_LOGO_FILES = frozenset({'logo.jpg', 'logo.jpeg', 'logo.png', 'logo.svg'})


def _serve_logo(filename):
    clean_name = (filename or '').strip()
    if (
        not clean_name
        or '/' in clean_name
        or '\\' in clean_name
        or '..' in clean_name
        or clean_name.lower() not in _ALLOWED_LOGO_FILES
    ):
        abort(404)

    new_logo = _get_new_logo_path()
    if new_logo:
        return send_file(new_logo, mimetype='image/png')

    search_dirs = [
        os.path.join(app.root_path, 'static', 'img'),
        os.path.join(app.root_path, 'public', 'img'),
        os.path.join(app.root_path, 'img')
    ]
    lower_name = clean_name.lower()
    for d in search_dirs:
        candidate = os.path.join(d, clean_name)
        if os.path.exists(candidate) and os.path.isfile(candidate):
            mimetype = (
                'image/jpeg' if lower_name.endswith(('.jpg', '.jpeg'))
                else ('image/png' if lower_name.endswith('.png') else 'image/svg+xml')
            )
            return send_from_directory(d, clean_name, mimetype=mimetype)
    return Response(_FALLBACK_LOGO_SVG, mimetype='image/svg+xml')


@app.route('/assets/<path:filename>')
def serve_public_assets(filename):
    assets_dir = os.path.join(app.root_path, 'public', 'assets')
    return send_from_directory(assets_dir, filename)


@app.route('/img/<path:filename>')
@app.route('/static/img/<path:filename>')
def serve_img(filename):
    return _serve_logo(filename)


@app.route('/favicon.ico')
@app.route('/favicon.png')
@app.route('/favicon.svg')
def serve_favicon():
    new_logo = _get_new_logo_path()
    if new_logo:
        return send_file(new_logo, mimetype='image/png')
    return Response(_FALLBACK_LOGO_SVG, mimetype='image/svg+xml')


@app.route('/logo.jpg')
@app.route('/logo.svg')
@app.route('/logo.png')
@app.route('/img/logo.jpg')
@app.route('/img/logo.svg')
@app.route('/img/logo.png')
def serve_logo_direct():
    return _serve_logo('logo.png')


# -------------------- РЕГИСТРАЦИЯ BLUEPRINTS --------------------
app.register_blueprint(main_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(cows_bp)
app.register_blueprint(reports_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(api_bp)
app.register_blueprint(stillborn_bp)


# -------------------- ЗАЩИТА ОТ DDOS И ПЕРЕГРУЗКИ СЕРВЕРА (SQLite) --------------------
def get_client_ip():
    """IP клиента определяется через ProxyFix (без доверия к подделываемым заголовкам)."""
    return request.remote_addr or '127.0.0.1'


def clear_all_rate_limits():
    """Очищает таблицу rate_limits (используется в тестах)."""
    run_write_transaction(lambda _conn, cur: cur.execute('DELETE FROM rate_limits'))


def check_rate_limit(ip, max_reqs=60, window_sec=60, burst_limit=20, burst_window=5, block_sec=30):
    """Атомарный учёт и проверка лимита запросов по IP в таблице SQLite rate_limits."""
    now = time.time()

    def _tx_rate(_conn, cur):
        cur.execute(
            'SELECT window_start, req_count, burst_start, burst_count, blocked_until FROM rate_limits WHERE ip = ?',
            (ip,)
        )
        row = cur.fetchone()

        if row and row['blocked_until'] > now:
            return False, max(1, int(row['blocked_until'] - now))

        expired_block = bool(row and row['blocked_until'] > 0)
        if not row or expired_block or (now - row['window_start'] >= window_sec):
            cur.execute(
                'DELETE FROM rate_limits WHERE window_start < ? AND blocked_until <= ?',
                (now - window_sec, now)
            )
            window_start = now
            req_count = 1
        else:
            window_start = row['window_start']
            req_count = (row['req_count'] or 0) + 1

        if not row or expired_block or (now - row['burst_start'] >= burst_window):
            burst_start = now
            burst_count = 1
        else:
            burst_start = row['burst_start']
            burst_count = (row['burst_count'] or 0) + 1

        is_blocked = (burst_limit and burst_count > burst_limit) or (req_count > max_reqs)
        blocked_until = (now + block_sec) if is_blocked else 0.0

        cur.execute('''
            INSERT INTO rate_limits (ip, window_start, req_count, burst_start, burst_count, blocked_until)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                window_start = excluded.window_start,
                req_count = excluded.req_count,
                burst_start = excluded.burst_start,
                burst_count = excluded.burst_count,
                blocked_until = excluded.blocked_until
        ''', (ip, window_start, req_count, burst_start, burst_count, blocked_until))

        if is_blocked:
            return False, block_sec
        return True, 0

    return run_write_transaction(_tx_rate)


_COMPRESSIBLE_PREFIXES = ('text/', 'application/json', 'application/javascript', 'image/svg+xml')
_STATIC_CACHE_PREFIXES = ('/static/', '/img/', '/assets/', '/favicon', '/logo.')


@app.before_request
def anti_ddos_guard():
    if request.method not in ('POST', 'PUT', 'DELETE') or request.path.startswith(_STATIC_CACHE_PREFIXES):
        return None
    if app.config.get('TESTING') and not os.environ.get('ENABLE_TEST_RATELIMIT'):
        return None

    client_ip = get_client_ip()
    if client_ip in ('127.0.0.1', '::1') and not os.environ.get('ENABLE_LOCAL_RATELIMIT'):
        return None

    ensure_app_initialized()
    allowed, wait_sec = check_rate_limit(client_ip, max_reqs=60, window_sec=60, burst_limit=20, burst_window=5, block_sec=30)
    if not allowed:
        return Response(
            f"429 Too Many Requests: Превышен лимит запросов. Подождите {wait_sec} сек.",
            status=429,
            mimetype="text/plain; charset=utf-8",
            headers={'Retry-After': str(wait_sec)}
        )

    return None


@app.before_request
def sync_current_user_session():
    if request.path.startswith(_STATIC_CACHE_PREFIXES):
        return None
    ensure_app_initialized()
    if 'user_id' in session:
        now = time.time()
        last_sync = session.get('_user_sync_ts', 0)
        if now - last_sync >= 60:
            user = get_user_by_id(session['user_id'])
            if user is None:
                session.clear()
                return redirect(url_for('auth.login'))
            session['farm_name'] = user.farm_name
            session['username'] = user.username
            session['user_type'] = user.user_type
            session['must_change_password'] = bool(user.must_change_password)
            session['_user_sync_ts'] = now

        if session.get('must_change_password') and request.path not in ('/first_login_password', '/logout'):
            return redirect(url_for('auth.first_login_password'))


@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if 'Server' in response.headers:
        del response.headers['Server']

    if request.path.startswith(_STATIC_CACHE_PREFIXES) and response.status_code == 200:
        response.headers['Cache-Control'] = 'public, max-age=86400'

    accept_encoding = request.headers.get('Accept-Encoding', '')
    mimetype = (response.mimetype or '').lower()
    if (
        'gzip' in accept_encoding.lower()
        and response.status_code == 200
        and not response.direct_passthrough
        and 'Content-Encoding' not in response.headers
        and any(mimetype.startswith(p) for p in _COMPRESSIBLE_PREFIXES)
    ):
        data = response.get_data()
        if len(data) >= 1024:
            compressed = gzip.compress(data, compresslevel=5)
            if len(compressed) < len(data):
                response.set_data(compressed)
                response.headers['Content-Encoding'] = 'gzip'
                response.headers['Content-Length'] = str(len(compressed))
                response.headers.add('Vary', 'Accept-Encoding')
                response.headers.pop('ETag', None)

    return response


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    reason = getattr(e, 'description', 'Недействительный или просроченный CSRF-токен.')
    is_ajax = (
        request.is_json
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or 'application/json' in request.headers.get('Accept', '')
    )
    if is_ajax:
        return jsonify({
            'status': 'error',
            'error': 'csrf_error',
            'message': f'Ошибка безопасности (CSRF): {reason}. Обновите страницу и повторите попытку.'
        }), 400
    return Response(
        f"<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'><title>Ошибка CSRF</title></head>"
        f"<body style='font-family:sans-serif;max-width:540px;margin:60px auto;padding:20px;'>"
        f"<h3>Сессия формы устарела</h3>"
        f"<p>Ошибка проверки безопасности (CSRF): {reason}</p>"
        f"<p><a href='{request.path}'>Обновить страницу</a> и повторить отправку.</p></body></html>",
        status=400,
        mimetype='text/html; charset=utf-8'
    )


@app.errorhandler(429)
def ratelimit_error(e):
    return Response("429 Too Many Requests: Превышен лимит запросов. Пожалуйста, подождите перед повторной попыткой.", status=429, mimetype="text/plain; charset=utf-8")


@app.errorhandler(413)
def payload_too_large(e):
    return Response("413 Request Entity Too Large: Размер отправленного запроса превышает допустимый лимит безопасности.", status=413, mimetype="text/plain; charset=utf-8")


@app.context_processor
def override_url_for():
    def has_endpoint(endpoint_name):
        if not endpoint_name:
            return False
        try:
            return (endpoint_name in app.view_functions) or (endpoint_name in app.url_map._rules_by_endpoint)
        except Exception:
            return False

    def safe_url_for(endpoint_name, default='#', **values):
        aliases = {
            'index': 'main.index',
            'dashboard': 'main.dashboard',
            'login': 'auth.login',
            'logout': 'auth.logout',
            'register': 'auth.register',
            'user_management': 'auth.user_management',
            'edit_user': 'auth.edit_user',
            'delete_user': 'auth.delete_user',
            'view_cows': 'cows.view_cows',
            'cows': 'cows.view_cows',
            'add_cow': 'cows.add_cow',
            'edit_cow': 'cows.edit_cow',
            'delete_cow': 'cows.delete_cow',
            'bulk_delete_cows': 'cows.bulk_delete_cows',
            'delete_multiple_cows': 'cows.delete_multiple_cows',
            'add_multiple_cows': 'cows.add_multiple_cows',
            'import_cows': 'cows.import_cows',
            'download_import_template': 'cows.download_import_template',
            'reports': 'reports.reports',
            'generate_report': 'reports.generate_report',
            'generate_original_excel_route': 'reports.generate_original_excel',
            'generate_original_excel': 'reports.generate_original_excel',
            'manage_reasons': 'admin.manage_reasons',
            'reasons': 'admin.manage_reasons',
            'toggle_reason': 'admin.toggle_reason',
            'delete_reason': 'admin.delete_reason',
            'audit_log': 'admin.audit_log',
            'backup_page': 'admin.backup',
            'backup': 'admin.backup',
            'download_db_backup': 'admin.download_backup',
            'restore_db_backup': 'admin.restore_backup',
            'settings': 'admin.settings',
            'get_farms': 'api.get_farms',
            'get_reasons': 'api.get_reasons',
            'stillborn': 'stillborn.stillborn_list',
            'stillborn_list': 'stillborn.stillborn_list',
            'add_stillborn': 'stillborn.add_stillborn',
            'edit_stillborn': 'stillborn.edit_stillborn',
            'delete_stillborn': 'stillborn.delete_stillborn',
            'export_stillborn_excel': 'stillborn.export_stillborn_excel'
        }
        resolved = aliases.get(endpoint_name, endpoint_name)
        try:
            return url_for(resolved, **values)
        except Exception as first_err:
            try:
                return url_for(endpoint_name, **values)
            except Exception as second_err:
                logger.error("Ошибка построения URL для endpoint=%r (%s / %s)", endpoint_name, first_err, second_err)
                if app.debug or app.testing:
                    raise
                return default

    def custom_url_for(endpoint, **values):
        return safe_url_for(endpoint, default='#', **values)

    custom_url_for.__doc__ = "custom_url_for helper function"
    try:
        ensure_app_initialized()
        settings = get_all_settings()
    except Exception:
        logger.exception("Не удалось загрузить настройки организации в context_processor")
        if app.debug or app.testing:
            raise
        settings = {}
    org_name = settings.get('farm_org_name') or Config.DEFAULT_ORG_NAME
    org_unp = settings.get('farm_unp') or Config.DEFAULT_UNP
    return dict(
        url_for=custom_url_for,
        has_endpoint=has_endpoint,
        safe_url_for=safe_url_for,
        org_name=org_name,
        org_unp=org_unp,
        current_year=datetime.now().year
    )


if __name__ == '__main__':
    ensure_app_initialized()
    port = int(os.environ.get('PORT') or os.environ.get('DEFAULT_APP_PORT') or 3000)
    is_debug = os.environ.get('FLASK_DEBUG', 'False').lower() in ('true', '1')
    app.run(host='0.0.0.0', port=port, debug=is_debug, threaded=True)

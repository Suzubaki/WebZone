# routes/auth.py - Аутентификация и управление пользователями (закрытая система)
import sqlite3
import time
from functools import wraps
from flask import Blueprint, abort, render_template, request, redirect, url_for, flash, session
from database import get_db_connection, log_audit_event, run_write_transaction
from auth import (
    authenticate_user, register_user, get_user_by_id,
    get_all_users, update_user, change_user_password
)

auth_bp = Blueprint('auth', __name__)

# Брутфорс-защита: хранение неудачных попыток входа по IP строго в SQLite (общая между воркерами WSGI)
_FAILED_WINDOW_SEC = 600


def clear_all_auth_lockouts():
    """Очищает таблицу блокировок авторизации (используется в тестах)."""
    run_write_transaction(lambda _conn, cur: cur.execute('DELETE FROM auth_lockouts'))


def _check_auth_lockout(client_ip):
    now = time.time()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('SELECT fail_count, last_attempt, locked_until FROM auth_lockouts WHERE ip = ?', (client_ip,))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return False, 0
    if row['locked_until'] > now:
        return True, max(1, int(row['locked_until'] - now))
    if now - row['last_attempt'] > _FAILED_WINDOW_SEC:
        run_write_transaction(
            lambda _conn, c: c.execute(
                'DELETE FROM auth_lockouts WHERE ip = ? AND locked_until <= ?',
                (client_ip, now)
            )
        )
    return False, 0


def _record_failed_auth(client_ip):
    now = time.time()

    def _tx_fail(_conn, cur):
        cur.execute('DELETE FROM auth_lockouts WHERE last_attempt < ? AND locked_until <= ?', (now - _FAILED_WINDOW_SEC, now))
        cur.execute('SELECT fail_count, last_attempt FROM auth_lockouts WHERE ip = ?', (client_ip,))
        row = cur.fetchone()
        if not row or (now - row['last_attempt'] > _FAILED_WINDOW_SEC):
            count = 1
        else:
            count = (row['fail_count'] or 0) + 1

        locked_until = 0.0
        if count >= 10:
            locked_until = now + 180
        elif count >= 8:
            locked_until = now + 60
        elif count >= 5:
            locked_until = now + 15

        cur.execute('''
            INSERT INTO auth_lockouts (ip, fail_count, last_attempt, locked_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                fail_count = excluded.fail_count,
                last_attempt = excluded.last_attempt,
                locked_until = excluded.locked_until
        ''', (client_ip, count, now, locked_until))

    run_write_transaction(_tx_fail)


def _clear_failed_auth(client_ip):
    run_write_transaction(lambda _conn, cur: cur.execute('DELETE FROM auth_lockouts WHERE ip = ?', (client_ip,)))


def login_required(f):
    """Декоратор проверки авторизации пользователя."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Пожалуйста, войдите в систему', 'warning')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Декоратор проверки прав администратора."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Пожалуйста, войдите в систему', 'warning')
            return redirect(url_for('auth.login'))
        if session.get('user_type') != 'admin':
            flash('Доступ запрещен. Требуются права администратора', 'danger')
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated_function


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Авторизация пользователя по учётным данным, выданным администратором."""
    if 'user_id' in session:
        if session.get('must_change_password'):
            return redirect(url_for('auth.first_login_password'))
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        client_ip = request.remote_addr or '127.0.0.1'

        locked, wait_sec = _check_auth_lockout(client_ip)
        if locked:
            flash(f'Слишком много неверных попыток авторизации с вашего IP. Повторите через {wait_sec} сек.', 'danger')
            return render_template('login.html'), 429

        user = authenticate_user(username, password)
        if user:
            _clear_failed_auth(client_ip)

            session['user_id'] = user.id
            session['username'] = user.username
            session['user_type'] = user.user_type
            session['farm_name'] = user.farm_name
            session['must_change_password'] = bool(user.must_change_password)
            session['_user_sync_ts'] = time.time()

            log_audit_event(
                username=user.username,
                farm_name=user.farm_name,
                action='LOGIN',
                entity_type='USER',
                entity_id=user.id,
                details='Успешная авторизация в системе',
                user_id=user.id,
                ip_address=client_ip
            )

            if user.must_change_password:
                flash('Для завершения первичного входа установите новый постоянный пароль.', 'warning')
                return redirect(url_for('auth.first_login_password'))

            flash(f'Добро пожаловать, {user.username}!', 'success')
            return redirect(url_for('main.dashboard'))
        else:
            _record_failed_auth(client_ip)
            flash('Неверное имя пользователя или пароль', 'danger')

    return render_template('login.html')


@auth_bp.route('/first_login_password', methods=['GET', 'POST'])
@login_required
def first_login_password():
    """Принудительная смена временного пароля при первом входе (если must_change_password=True)."""
    user = get_user_by_id(session.get('user_id'))
    if not user or not user.must_change_password:
        abort(404)

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if new_password != confirm_password:
            flash('Новый пароль и подтверждение не совпадают', 'danger')
            return render_template('first_login_password.html', username=user.username), 400

        if user.check_password(new_password):
            flash('Новый пароль должен отличаться от временного пароля', 'danger')
            return render_template('first_login_password.html', username=user.username), 400

        ok, msg = change_user_password(user.id, new_password)
        if not ok:
            flash(msg, 'danger')
            return render_template('first_login_password.html', username=user.username), 400

        session['must_change_password'] = False
        session['_user_sync_ts'] = time.time()
        log_audit_event(
            username=user.username,
            farm_name=user.farm_name or 'Admin',
            action='PASSWORD_CHANGE',
            entity_type='USER',
            entity_id=user.id,
            details='Выполнена обязательная смена первичного пароля',
            user_id=user.id,
            ip_address=request.remote_addr
        )
        flash('Новый постоянный пароль установлен. Добро пожаловать в систему!', 'success')
        return redirect(url_for('main.dashboard'))

    return render_template('first_login_password.html', username=user.username)


@auth_bp.route('/logout', methods=['POST'])
def logout():
    """Завершение пользовательской сессии (строго POST с CSRF-защитой)."""
    username = session.get('username', 'Unknown')
    user_id = session.get('user_id')
    farm_name = session.get('farm_name')

    if user_id:
        log_audit_event(
            username=username,
            farm_name=farm_name,
            action='LOGOUT',
            entity_type='USER',
            entity_id=user_id,
            details='Выход из системы',
            user_id=user_id,
            ip_address=request.remote_addr
        )

    session.clear()
    flash('Вы вышли из системы', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/register', methods=['GET', 'POST'])
@admin_required
def register():
    """Регистрация новой учётной записи (только администратор)."""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user_type = request.form.get('user_type')
        farm_name = request.form.get('farm_name', '').strip() if user_type == 'farm' else None

        success, message = register_user(username, password, user_type, farm_name)
        if success:
            log_audit_event(
                username=session.get('username'),
                farm_name='Admin',
                action='CREATE_USER',
                entity_type='USER',
                details=f"Зарегистрирован пользователь {username} ({user_type}, ферма: {farm_name or '-'})",
                user_id=session.get('user_id'),
                ip_address=request.remote_addr
            )
            flash(message, 'success')
            return redirect(url_for('auth.user_management'))
        else:
            flash(message, 'danger')

    return render_template('register.html')


@auth_bp.route('/user_management')
@admin_required
def user_management():
    """Панель администрирования пользователей."""
    users = get_all_users()
    return render_template('user_management.html', users=users)


@auth_bp.route('/edit_user/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def edit_user(user_id):
    """Редактирование профиля и пароля пользователя администратором."""
    user = get_user_by_id(user_id)
    if not user:
        flash('Пользователь не найден', 'danger')
        return redirect(url_for('auth.user_management'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user_type = request.form.get('user_type')
        farm_name = request.form.get('farm_name', '').strip() if user_type == 'farm' else None

        success, message = update_user(
            user_id,
            username,
            password if password else None,
            user_type,
            farm_name
        )
        if success:
            log_audit_event(
                username=session.get('username'),
                farm_name='Admin',
                action='UPDATE_USER',
                entity_type='USER',
                entity_id=user_id,
                details=f"Обновлен профиль пользователя {username}",
                user_id=session.get('user_id'),
                ip_address=request.remote_addr
            )
            flash(message, 'success')
            return redirect(url_for('auth.user_management'))
        else:
            flash(message, 'danger')

    return render_template('edit_user.html', user=user)


@auth_bp.route('/delete_user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    """Удаление пользователя (исторические записи выбытия сохраняются)."""
    if user_id == session.get('user_id'):
        flash('Вы не можете удалить свою собственную учетную запись', 'danger')
        return redirect(url_for('auth.user_management'))

    actor_username = session.get('username')
    actor_id = session.get('user_id')
    client_ip = request.remote_addr

    def _tx_del_user(conn, cursor):
        cursor.execute('SELECT id, username, user_type FROM users WHERE id = ?', (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        target_username = row['username']
        target_type = row['user_type']
        cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        log_audit_event(
            username=actor_username,
            farm_name='Admin',
            action='DELETE_USER',
            entity_type='USER',
            entity_id=user_id,
            details=f"Удален пользователь {target_username} ({target_type}). Записи выбытия скота сохранены.",
            user_id=actor_id,
            ip_address=client_ip,
            cursor=cursor
        )
        return target_username

    deleted_username = run_write_transaction(_tx_del_user)
    if deleted_username:
        flash(f'Пользователь {deleted_username} успешно удален (исторические записи фермы сохранены)', 'success')
    else:
        flash('Пользователь не найден', 'danger')

    return redirect(url_for('auth.user_management'))

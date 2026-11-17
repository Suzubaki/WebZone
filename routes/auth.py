# routes/auth.py - Аутентификация и управление пользователями
import time
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from database import get_db_connection, log_audit_event
from auth import (
    authenticate_user, register_user, get_user_by_id,
    get_all_users, update_user, change_user_password
)

auth_bp = Blueprint('auth', __name__)

# Брутфорс-защита: отслеживание неудачных попыток входа ТОЛЬКО по IP
# (блокировка по username убрана во избежание DoS-блокировки чужих учётных записей)
_failed_logins = {}
_FAILED_WINDOW_SEC = 600
_MAX_TRACKED_IPS = 2000


def _prune_failed_logins(now):
    if len(_failed_logins) <= _MAX_TRACKED_IPS:
        return
    stale = [
        ip for ip, rec in _failed_logins.items()
        if (now - rec.get('last', 0) > _FAILED_WINDOW_SEC) and rec.get('locked_until', 0) <= now
    ]
    for ip in stale:
        _failed_logins.pop(ip, None)


def _check_auth_lockout(client_ip):
    rec = _failed_logins.get(client_ip)
    if not rec:
        return False, 0
    now = time.time()
    locked_until = rec.get('locked_until', 0)
    if locked_until > now:
        return True, max(1, int(locked_until - now))
    if now - rec.get('last', 0) > _FAILED_WINDOW_SEC:
        _failed_logins.pop(client_ip, None)
    return False, 0


def _record_failed_auth(client_ip):
    now = time.time()
    _prune_failed_logins(now)
    rec = _failed_logins.get(client_ip)
    if not rec or (now - rec['last'] > _FAILED_WINDOW_SEC):
        rec = {'count': 1, 'last': now, 'locked_until': 0}
    else:
        rec['count'] += 1
        rec['last'] = now

    # Экспоненциальная блокировка без блокирующего time.sleep() в потоке воркера
    if rec['count'] >= 10:
        rec['locked_until'] = now + 180
    elif rec['count'] >= 8:
        rec['locked_until'] = now + 60
    elif rec['count'] >= 5:
        rec['locked_until'] = now + 15
    _failed_logins[client_ip] = rec


def _clear_failed_auth(client_ip):
    _failed_logins.pop(client_ip, None)


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
    """Авторизация пользователя в системе."""
    if 'user_id' in session:
        if session.get('must_change_password'):
            return redirect(url_for('auth.change_password'))
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
                flash('Для продолжения работы необходимо сменить временный пароль.', 'warning')
                return redirect(url_for('auth.change_password'))

            flash(f'Добро пожаловать, {user.username}!', 'success')
            return redirect(url_for('main.dashboard'))
        else:
            _record_failed_auth(client_ip)
            flash('Неверное имя пользователя или пароль', 'danger')

    return render_template('login.html')


@auth_bp.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    """Смена собственного пароля (в т.ч. обязательная при первом входе)."""
    user = get_user_by_id(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not user.check_password(current_password):
            flash('Текущий пароль указан неверно', 'danger')
            return render_template('change_password.html', forced=bool(session.get('must_change_password')))

        if len(new_password) < 8:
            flash('Новый пароль должен содержать не менее 8 символов', 'danger')
            return render_template('change_password.html', forced=bool(session.get('must_change_password')))

        if new_password != confirm_password:
            flash('Новый пароль и подтверждение не совпадают', 'danger')
            return render_template('change_password.html', forced=bool(session.get('must_change_password')))

        if new_password == current_password:
            flash('Новый пароль должен отличаться от текущего', 'danger')
            return render_template('change_password.html', forced=bool(session.get('must_change_password')))

        ok, message = change_user_password(user.id, new_password)
        if ok:
            session['must_change_password'] = False
            session['_user_sync_ts'] = time.time()
            log_audit_event(
                username=user.username,
                farm_name=user.farm_name,
                action='CHANGE_PASSWORD',
                entity_type='USER',
                entity_id=user.id,
                details='Пользователь обновил пароль',
                user_id=user.id,
                ip_address=request.remote_addr
            )
            flash('Пароль успешно изменён', 'success')
            return redirect(url_for('main.dashboard'))
        flash(message, 'danger')

    return render_template('change_password.html', forced=bool(session.get('must_change_password')))


@auth_bp.route('/logout')
def logout():
    """Завершение пользовательской сессии."""
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
        must_change = request.form.get('must_change_password') in ('on', '1', 'true', 'True')

        success, message = register_user(
            username, password, user_type, farm_name, must_change_password=must_change
        )
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
    """Редактирование профиля пользователя."""
    user = get_user_by_id(user_id)
    if not user:
        flash('Пользователь не найден', 'danger')
        return redirect(url_for('auth.user_management'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user_type = request.form.get('user_type')
        farm_name = request.form.get('farm_name', '').strip() if user_type == 'farm' else None
        must_change = request.form.get('must_change_password') in ('on', '1', 'true', 'True')

        success, message = update_user(
            user_id,
            username,
            password if password else None,
            user_type,
            farm_name,
            must_change_password=must_change
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

    user = get_user_by_id(user_id)
    if user:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()
        conn.close()

        log_audit_event(
            username=session.get('username'),
            farm_name='Admin',
            action='DELETE_USER',
            entity_type='USER',
            entity_id=user_id,
            details=f"Удален пользователь {user.username} ({user.user_type}). Записи выбытия скота сохранены.",
            user_id=session.get('user_id'),
            ip_address=request.remote_addr
        )
        flash(f'Пользователь {user.username} успешно удален (исторические записи фермы сохранены)', 'success')
    else:
        flash('Пользователь не найден', 'danger')

    return redirect(url_for('auth.user_management'))

# auth.py - Аутентификация и управление пользователями
import sqlite3
from config import Config
from database import get_db_connection, run_write_transaction
from models import User


def validate_password_policy(password):
    """Единая проверка надёжности пароля для регистрации, смены и админ-обновления."""
    min_len = getattr(Config, 'PASSWORD_MIN_LENGTH', 8)
    if not password or len(password) < min_len:
        return False, f'Пароль должен содержать не менее {min_len} символов'
    if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
        return False, 'Пароль должен содержать хотя бы одну букву и одну цифру'
    return True, ''


def _row_to_user(row):
    keys = row.keys()
    return User(
        id=row['id'],
        username=row['username'],
        password_hash=row['password_hash'],
        user_type=row['user_type'],
        farm_name=row['farm_name'],
        must_change_password=bool(row['must_change_password']) if 'must_change_password' in keys else False,
        session_version=int(row['session_version'] or 1) if 'session_version' in keys else 1,
        created_at=row['created_at']
    )


def authenticate_user(username, password):
    """
    Проверка учетных данных пользователя.
    Возвращает объект User или None.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
    user_data = cursor.fetchone()
    conn.close()

    if user_data:
        user = _row_to_user(user_data)
        if user.check_password(password):
            return user
    return None


def get_user_by_id(user_id):
    """Получение пользователя по ID."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
    user_data = cursor.fetchone()
    conn.close()

    if user_data:
        return _row_to_user(user_data)
    return None


def register_user(username, password, user_type, farm_name=None, must_change_password=False):
    """
    Регистрация нового пользователя.
    Возвращает (success, message).
    """
    username = (username or '').strip()
    if not username:
        return False, 'Укажите имя пользователя'
    pwd_ok, pwd_msg = validate_password_policy(password)
    if not pwd_ok:
        return False, pwd_msg
    if user_type not in ('admin', 'farm'):
        return False, 'Недопустимый тип пользователя'
    clean_farm = (farm_name or '').strip()
    if user_type == 'farm' and (not clean_farm or clean_farm.lower() == 'all'):
        return False, 'Для учётной записи фермы обязательно указать название подразделения'

    def _tx_register(_conn, cursor):
        cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
        if cursor.fetchone():
            return False, 'Пользователь с таким именем уже существует'

        password_hash = User.create_password_hash(password)
        flag = 1 if must_change_password else 0

        cursor.execute('''
            INSERT INTO users (username, password_hash, user_type, farm_name, must_change_password, session_version)
            VALUES (?, ?, ?, ?, ?, 1)
        ''', (username, password_hash, user_type, (farm_name or '').strip() or None, flag))
        return True, 'Пользователь успешно зарегистрирован'

    try:
        return run_write_transaction(_tx_register)
    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'


def change_user_password(user_id, new_password):
    """Смена пароля пользователем и снятие флага обязательной смены пароля."""
    pwd_ok, pwd_msg = validate_password_policy(new_password)
    if not pwd_ok:
        return False, pwd_msg

    password_hash = User.create_password_hash(new_password)
    try:
        run_write_transaction(
            lambda _conn, cursor: cursor.execute(
                'UPDATE users SET password_hash = ?, must_change_password = 0, session_version = COALESCE(session_version, 1) + 1 WHERE id = ?',
                (password_hash, user_id)
            )
        )
        return True, 'Пароль успешно обновлён'
    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'


def get_all_users():
    """Получение списка всех пользователей."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM users ORDER BY created_at DESC')
    users_data = cursor.fetchall()
    conn.close()

    return [_row_to_user(row) for row in users_data]


def update_user(user_id, username, password=None, user_type=None, farm_name=None, must_change_password=None):
    """
    Обновление данных пользователя. При смене farm_name обновляются
    ТОЛЬКО записи, принадлежащие этому пользователю (created_by = user_id),
    чтобы не затрагивать записи других пользователей той же фермы.
    """
    def _tx_update(_conn, cursor):
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = cursor.fetchone()
        if not user:
            return False, 'Пользователь не найден'

        clean_username = (username or '').strip()
        if not clean_username:
            return False, 'Укажите имя пользователя'

        old_farm_name = user['farm_name']
        effective_user_type = user_type or user['user_type']
        if effective_user_type not in ('admin', 'farm'):
            return False, 'Недопустимый тип пользователя'

        if effective_user_type == 'farm':
            raw_farm = farm_name if farm_name is not None else (old_farm_name or '')
            clean_farm = raw_farm.strip()
            if not clean_farm or clean_farm.lower() == 'all':
                return False, 'Для учётной записи фермы обязательно указать название подразделения'
        else:
            clean_farm = None

        if clean_username != user['username']:
            cursor.execute('SELECT id FROM users WHERE username = ? AND id != ?', (clean_username, user_id))
            if cursor.fetchone():
                return False, 'Пользователь с таким именем уже существует'

        update_fields = ['username = ?']
        params = [clean_username]
        bump_session_version = False

        if password:
            pwd_ok, pwd_msg = validate_password_policy(password)
            if not pwd_ok:
                return False, pwd_msg
            password_hash = User.create_password_hash(password)
            update_fields.append('password_hash = ?')
            params.append(password_hash)
            bump_session_version = True

        if must_change_password is not None:
            update_fields.append('must_change_password = ?')
            params.append(1 if must_change_password else 0)

        if user_type:
            if user_type != user['user_type']:
                bump_session_version = True
            update_fields.append('user_type = ?')
            params.append(user_type)

        if effective_user_type == 'farm':
            update_fields.append('farm_name = ?')
            params.append(clean_farm)
        elif user_type == 'admin':
            update_fields.append('farm_name = ?')
            params.append(None)

        if bump_session_version:
            update_fields.append('session_version = COALESCE(session_version, 1) + 1')

        params.append(user_id)

        query = f"UPDATE users SET {', '.join(update_fields)} WHERE id = ?"
        cursor.execute(query, params)

        if clean_farm and old_farm_name and clean_farm != old_farm_name:
            cursor.execute(
                'UPDATE cows SET farm_name = ? WHERE created_by = ? AND farm_name = ?',
                (clean_farm, user_id, old_farm_name)
            )
            cursor.execute(
                'UPDATE stillbirth_records SET farm_name = ? WHERE created_by = ? AND farm_name = ?',
                (clean_farm, user_id, old_farm_name)
            )
            cursor.execute(
                'UPDATE audit_logs SET farm_name = ? WHERE user_id = ? AND farm_name = ?',
                (clean_farm, user_id, old_farm_name)
            )

            cursor.execute('SELECT COUNT(*) as cnt FROM users WHERE farm_name = ? AND id != ?', (old_farm_name, user_id))
            other_users_cnt = cursor.fetchone()['cnt']
            cursor.execute('SELECT COUNT(*) as cnt FROM cows WHERE farm_name = ?', (old_farm_name,))
            rem_cows_cnt = cursor.fetchone()['cnt']
            cursor.execute('SELECT COUNT(*) as cnt FROM stillbirth_records WHERE farm_name = ?', (old_farm_name,))
            rem_sb_cnt = cursor.fetchone()['cnt']

            if other_users_cnt == 0 and rem_cows_cnt == 0 and rem_sb_cnt == 0:
                cursor.execute('SELECT last_cow_number, last_stillborn_number FROM farm_counters WHERE farm_name = ?', (old_farm_name,))
                old_cnt = cursor.fetchone()
                if old_cnt:
                    cursor.execute('SELECT last_cow_number, last_stillborn_number FROM farm_counters WHERE farm_name = ?', (clean_farm,))
                    new_cnt = cursor.fetchone()
                    if new_cnt:
                        merged_cow = max(old_cnt['last_cow_number'] or 0, new_cnt['last_cow_number'] or 0)
                        merged_sb = max(old_cnt['last_stillborn_number'] or 0, new_cnt['last_stillborn_number'] or 0)
                        cursor.execute(
                            'UPDATE farm_counters SET last_cow_number = ?, last_stillborn_number = ? WHERE farm_name = ?',
                            (merged_cow, merged_sb, clean_farm)
                        )
                        cursor.execute('DELETE FROM farm_counters WHERE farm_name = ?', (old_farm_name,))
                    else:
                        cursor.execute('UPDATE farm_counters SET farm_name = ? WHERE farm_name = ?', (clean_farm, old_farm_name))
        elif clean_farm and not old_farm_name:
            cursor.execute('UPDATE cows SET farm_name = ? WHERE created_by = ? AND (farm_name IS NULL OR farm_name = "")', (clean_farm, user_id))
            cursor.execute('UPDATE stillbirth_records SET farm_name = ? WHERE created_by = ? AND (farm_name IS NULL OR farm_name = "")', (clean_farm, user_id))

        return True, 'Данные пользователя и связанные записи успешно обновлены'

    try:
        return run_write_transaction(_tx_update)
    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'


def delete_user(user_id):
    """Удаление пользователя."""
    try:
        run_write_transaction(
            lambda _conn, cursor: cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        )
        return True, 'Пользователь успешно удален'
    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'

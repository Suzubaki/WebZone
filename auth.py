# auth.py - Аутентификация и управление пользователями
import sqlite3
from database import get_db_connection
from models import User


def _row_to_user(row):
    keys = row.keys()
    return User(
        id=row['id'],
        username=row['username'],
        password_hash=row['password_hash'],
        user_type=row['user_type'],
        farm_name=row['farm_name'],
        must_change_password=bool(row['must_change_password']) if 'must_change_password' in keys else False,
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
    if not password or len(password) < 6:
        return False, 'Пароль должен содержать не менее 6 символов'
    if user_type not in ('admin', 'farm'):
        return False, 'Недопустимый тип пользователя'
    if user_type == 'farm' and not (farm_name or '').strip():
        return False, 'Для учётной записи фермы обязательно указать название подразделения'

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT id FROM users WHERE username = ?', (username,))
        if cursor.fetchone():
            return False, 'Пользователь с таким именем уже существует'

        password_hash = User.create_password_hash(password)
        flag = 1 if must_change_password else 0

        cursor.execute('''
            INSERT INTO users (username, password_hash, user_type, farm_name, must_change_password)
            VALUES (?, ?, ?, ?, ?)
        ''', (username, password_hash, user_type, (farm_name or '').strip() or None, flag))

        conn.commit()
        return True, 'Пользователь успешно зарегистрирован'

    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'
    finally:
        conn.close()


def change_user_password(user_id, new_password):
    """Смена пароля пользователем и снятие флага обязательной смены пароля."""
    if not new_password or len(new_password) < 8:
        return False, 'Новый пароль должен содержать не менее 8 символов'

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        password_hash = User.create_password_hash(new_password)
        cursor.execute(
            'UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?',
            (password_hash, user_id)
        )
        conn.commit()
        return True, 'Пароль успешно обновлён'
    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'
    finally:
        conn.close()


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
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        user = cursor.fetchone()
        if not user:
            return False, 'Пользователь не найден'

        old_farm_name = user['farm_name']

        if username != user['username']:
            cursor.execute('SELECT id FROM users WHERE username = ? AND id != ?', (username, user_id))
            if cursor.fetchone():
                return False, 'Пользователь с таким именем уже существует'

        update_fields = ['username = ?']
        params = [username]

        if password:
            if len(password) < 6:
                return False, 'Пароль должен содержать не менее 6 символов'
            password_hash = User.create_password_hash(password)
            update_fields.append('password_hash = ?')
            params.append(password_hash)

        if must_change_password is not None:
            update_fields.append('must_change_password = ?')
            params.append(1 if must_change_password else 0)

        if user_type:
            update_fields.append('user_type = ?')
            params.append(user_type)

        if farm_name is not None:
            clean_farm = farm_name.strip() or None
            update_fields.append('farm_name = ?')
            params.append(clean_farm)
        else:
            clean_farm = None

        params.append(user_id)

        query = f"UPDATE users SET {', '.join(update_fields)} WHERE id = ?"
        cursor.execute(query, params)

        if clean_farm and old_farm_name and clean_farm != old_farm_name:
            # Перепривязываем только записи, созданные именно этим пользователем
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

            # Переносим счётчик только если старое название фермы больше никем не используется
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

        conn.commit()
        return True, 'Данные пользователя и связанные записи успешно обновлены'

    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'
    finally:
        conn.close()


def delete_user(user_id):
    """Удаление пользователя."""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()
        return True, 'Пользователь успешно удален'
    except sqlite3.Error as e:
        return False, f'Ошибка базы данных: {str(e)}'
    finally:
        conn.close()

# create_users.py - Начальная инициализация администратора (только при пустой таблице users)
import os
import secrets
import sys
from config import Config
from database import init_db, get_db_connection
from auth import register_user


def _write_initial_credentials(lines):
    instance_dir = os.path.join(Config.BASE_DIR, 'instance')
    cred_path = os.path.join(instance_dir, 'initial_credentials.txt')
    try:
        os.makedirs(instance_dir, mode=0o700, exist_ok=True)
        fd = os.open(cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return cred_path
    except OSError:
        return None


def _generate_compliant_password(nbytes=12):
    """Генерирует случайный пароль, гарантированно удовлетворяющий политике паролей."""
    token = secrets.token_urlsafe(nbytes)
    if not any(ch.isalpha() for ch in token):
        token += 'A'
    if not any(ch.isdigit() for ch in token):
        token += '7'
    return token


def create_default_users(include_demo_farms=False):
    """
    Создаёт первичного администратора ТОЛЬКО если таблица users пуста.
    Никакие захардкоженные пароли не записываются: пароль берётся из
    INITIAL_ADMIN_PASSWORD либо генерируется криптографически случайно,
    с обязательной сменой при первом входе (must_change_password = 1).
    """
    init_db()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) as cnt FROM users')
    existing_count = cursor.fetchone()['cnt']
    conn.close()

    if existing_count > 0:
        return

    admin_pwd = (os.environ.get('INITIAL_ADMIN_PASSWORD') or '').strip()
    generated = False
    if not admin_pwd:
        admin_pwd = _generate_compliant_password(12)
        generated = True

    ok, msg = register_user('admin', admin_pwd, 'admin', must_change_password=True)
    lines = ["Создан первичный администратор: username=admin (потребуется смена пароля при первом входе)"]
    if generated and ok:
        lines.append(f"Временный пароль admin: {admin_pwd}")

    if include_demo_farms and ok:
        for idx in (1, 2):
            f_pwd = _generate_compliant_password(10)
            f_ok, _ = register_user(f'farm{idx}', f_pwd, 'farm', f'Ферма {idx}', must_change_password=True)
            if f_ok:
                lines.append(f"Временный пароль farm{idx} (Ферма {idx}): {f_pwd}")

    if ok and generated:
        cred_path = _write_initial_credentials(lines)
        print("=" * 60)
        for line in lines:
            print(line)
        if cred_path:
            print(f"Сохранено локально в {cred_path} (удалите файл после входа)")
        print("=" * 60)
    elif not ok:
        print(f"Инициализация admin: {msg}")


if __name__ == '__main__':
    create_default_users(include_demo_farms=('--with-demo-farms' in sys.argv))

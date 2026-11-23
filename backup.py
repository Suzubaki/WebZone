# backup.py - Модуль резервного копирования и восстановления базы данных
import logging
import os
import tempfile
import shutil
import sqlite3
from datetime import datetime
from config import Config
from database import (
    _write_lock,
    log_audit_event,
    init_db,
    reset_connection_state,
    invalidate_settings_cache,
)

logger = logging.getLogger('cattle_app')


def _sqlite_online_backup(src_path, dst_path):
    """Выполняет атомарное копирование SQLite-базы (включая страницы WAL) через Online Backup API."""
    src = sqlite3.connect(src_path, timeout=15.0)
    dst = sqlite3.connect(dst_path, timeout=15.0)
    try:
        src.execute('PRAGMA busy_timeout=15000;')
        dst.execute('PRAGMA busy_timeout=15000;')
        src.backup(dst, pages=256, sleep=0.05)
    finally:
        try:
            dst.close()
        finally:
            src.close()


def _remove_db_and_sidecars(db_path, ignore_errors=False):
    for suffix in ('', '-wal', '-shm'):
        target = db_path + suffix
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                if not ignore_errors:
                    raise


def create_db_backup_bytes():
    """
    Создает целостную резервную копию базы данных SQLite с использованием SQLite Online Backup API.
    Возвращает (bytes, filename).
    """
    source_db_path = os.path.abspath(Config.DATABASE)
    if not os.path.exists(source_db_path):
        init_db()

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"agro_backup_RB_{timestamp}.sqlite"

    db_dir = os.path.dirname(source_db_path) or '.'
    try:
        os.makedirs(db_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix='backup_', suffix='.sqlite', dir=db_dir)
    except OSError:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix='backup_', suffix='.sqlite')
    os.close(tmp_fd)

    try:
        with _write_lock:
            _sqlite_online_backup(source_db_path, tmp_path)
        with open(tmp_path, 'rb') as f:
            file_bytes = f.read()
    finally:
        _remove_db_and_sidecars(tmp_path, ignore_errors=True)

    return file_bytes, filename


def restore_db_from_upload(upload_file, username=None, ip_address=None):
    """
    Безопасное восстановление базы данных из загруженного файла .db/.sqlite:
    - проверка размера и заголовка SQLite;
    - полная проверка целостности (PRAGMA integrity_check) и обязательных таблиц;
    - блокировка записи (_write_lock) на время замены;
    - аварийная резервная копия текущей БД через SQLite Online Backup API (с учётом WAL);
    - восстановление через SQLite Online Backup API и гарантированный откат при сбое.
    """
    temp_path = None
    db_path = os.path.abspath(Config.DATABASE)
    pre_restore_bak = db_path + '.pre_restore_bak'
    max_bytes = getattr(Config, 'BACKUP_MAX_CONTENT_LENGTH', 50 * 1024 * 1024)

    try:
        upload_file.seek(0, os.SEEK_END)
        file_size = upload_file.tell()
        upload_file.seek(0)

        if file_size > max_bytes:
            limit_mb = max_bytes // (1024 * 1024)
            return False, f'Файл резервной копии слишком велик (максимум {limit_mb} МБ)'
        if file_size < 100:
            return False, 'Файл резервной копии поврежден или пуст'

        header = upload_file.read(16)
        if not header.startswith(b'SQLite format 3'):
            upload_file.seek(0)
            return False, 'Загруженный файл не является корректной базой данных SQLite'
        upload_file.seek(0)

        db_dir = os.path.dirname(db_path) or '.'
        os.makedirs(db_dir, exist_ok=True)
        tmp_fd, temp_path = tempfile.mkstemp(prefix='restore_', suffix='.sqlite', dir=db_dir)
        os.close(tmp_fd)
        upload_file.save(temp_path)

        # Проверяем целостность и наличие обязательных таблиц до захвата блокировки записи
        test_conn = sqlite3.connect(temp_path, timeout=15.0)
        try:
            test_cursor = test_conn.cursor()
            test_cursor.execute("PRAGMA integrity_check")
            check_result = test_cursor.fetchone()
            if not check_result or check_result[0] != 'ok':
                return False, 'Файл резервной копии поврежден (PRAGMA integrity_check failed)'

            test_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in test_cursor.fetchall()}
        finally:
            test_conn.close()

        if 'cows' not in tables or 'users' not in tables:
            return False, 'В резервной копии отсутствуют обязательные таблицы (cows, users)'

        with _write_lock:
            has_pre_backup = False
            if os.path.exists(db_path):
                try:
                    _remove_db_and_sidecars(pre_restore_bak, ignore_errors=True)
                    _sqlite_online_backup(db_path, pre_restore_bak)
                    has_pre_backup = True
                except sqlite3.DatabaseError:
                    # Если текущая БД на диске повреждена, сохраняем сырую копию перед очисткой
                    shutil.copy2(db_path, pre_restore_bak)
                    has_pre_backup = True

            try:
                try:
                    _sqlite_online_backup(temp_path, db_path)
                except sqlite3.DatabaseError:
                    # Fallback при повреждении исходной БД: удаляем БД и -wal/-shm и заменяем файлом
                    _remove_db_and_sidecars(db_path)
                    shutil.copy2(temp_path, db_path)

                reset_connection_state()
                invalidate_settings_cache()
                init_db()
                invalidate_settings_cache()
            except Exception as restore_err:
                logger.exception("Сбой восстановления БД, выполняется откат к аварийной копии")
                if has_pre_backup and os.path.exists(pre_restore_bak):
                    try:
                        _sqlite_online_backup(pre_restore_bak, db_path)
                    except sqlite3.DatabaseError:
                        _remove_db_and_sidecars(db_path, ignore_errors=True)
                        shutil.copy2(pre_restore_bak, db_path)
                    reset_connection_state()
                    invalidate_settings_cache()
                return False, f'Ошибка при восстановлении базы данных (выполнен откат): {str(restore_err)}'

        log_audit_event(
            username=username or 'Admin',
            farm_name='Admin',
            action='BACKUP',
            entity_type='SETTINGS',
            details='Выполнено восстановление базы данных из резервной копии',
            ip_address=ip_address
        )
        return True, 'База данных успешно восстановлена из резервной копии'

    except Exception as e:
        logger.exception("Ошибка при восстановлении базы данных")
        return False, f'Ошибка при восстановлении базы данных: {str(e)}'
    finally:
        if temp_path:
            _remove_db_and_sidecars(temp_path, ignore_errors=True)

# scheduled_backup.py - Автоматическое плановое резервное копирование базы данных SQLite
import argparse
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backup import create_db_backup_bytes
from config import Config
from database import init_db, log_audit_event

logger = logging.getLogger('cattle_app')

_BACKUP_NAME_RE = re.compile(r'^agro_backup_RB_(\d{8})_(\d{6})\.sqlite$')
_AUTO_CHECK_INTERVAL_SEC = 3600
_auto_backup_lock = threading.Lock()
_last_check_by_target = {}


def get_backup_dir(backup_dir=None):
    """Возвращает абсолютный путь к каталогу автоматических резервных копий."""
    return os.path.abspath(backup_dir or os.path.join(Config.BASE_DIR, 'backups'))


def is_valid_backup_filename(filename):
    """Строгая проверка имени файла архива (защита от Path Traversal)."""
    return bool(filename and _BACKUP_NAME_RE.match(filename))


def _find_existing_period_backup(backup_dir, period, now):
    """Ищет уже созданный бэкап за текущую неделю (ISO) или текущий месяц в папке backups."""
    if not os.path.isdir(backup_dir):
        return None
    target_week = now.isocalendar()[:2]
    target_month = (now.year, now.month)

    for fname in sorted(os.listdir(backup_dir)):
        m = _BACKUP_NAME_RE.match(fname)
        if not m:
            continue
        fpath = os.path.join(backup_dir, fname)
        if not (os.path.isfile(fpath) and os.path.getsize(fpath) > 0):
            continue
        try:
            fdate = datetime.strptime(m.group(1), '%Y%m%d')
        except ValueError:
            continue
        if period == 'weekly' and fdate.isocalendar()[:2] == target_week:
            return fpath
        if period == 'monthly' and (fdate.year, fdate.month) == target_month:
            return fpath
    return None


def _prune_old_backups(backup_dir, keep_count):
    """Удаляет самые старые резервные копии, оставляя последние keep_count файлов."""
    if not keep_count or keep_count <= 0 or not os.path.isdir(backup_dir):
        return []
    files = [
        fname for fname in sorted(os.listdir(backup_dir))
        if _BACKUP_NAME_RE.match(fname)
        and os.path.isfile(os.path.join(backup_dir, fname))
    ]
    if len(files) <= keep_count:
        return []
    removed = []
    for old_name in files[:-keep_count]:
        old_path = os.path.join(backup_dir, old_name)
        try:
            os.remove(old_path)
            removed.append(old_name)
        except OSError:
            pass
    return removed


def list_saved_backups(backup_dir=None):
    """Возвращает список сохранённых резервных копий (от новых к старым) для панели администратора."""
    target_dir = get_backup_dir(backup_dir)
    if not os.path.isdir(target_dir):
        return []
    items = []
    for fname in sorted(os.listdir(target_dir), reverse=True):
        m = _BACKUP_NAME_RE.match(fname)
        if not m:
            continue
        fpath = os.path.join(target_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            size_bytes = os.path.getsize(fpath)
            dt = datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S')
            created_str = dt.strftime('%d.%m.%Y %H:%M:%S')
        except (OSError, ValueError):
            size_bytes = 0
            created_str = fname
        if size_bytes >= 1024 * 1024:
            size_label = f"{size_bytes / (1024 * 1024):.2f} МБ"
        else:
            size_label = f"{max(1, size_bytes // 1024)} КБ"
        items.append({
            'filename': fname,
            'created_at': created_str,
            'size_bytes': size_bytes,
            'size_label': size_label,
        })
    return items


def run_scheduled_backup(backup_dir=None, period='weekly', monthly=None, keep=52, force=False):
    """
    Создаёт резервную копию БД в папке backup_dir.
    По умолчанию period='weekly', keep=52 (1 год еженедельного хранения) —
    создаёт не более одной копии в календарную неделю (ISO), без дублей.
    """
    if monthly is True:
        period = 'monthly'
    elif monthly is False and period not in ('weekly', 'monthly'):
        period = 'weekly'

    target_dir = get_backup_dir(backup_dir)
    os.makedirs(target_dir, exist_ok=True)

    now = datetime.now()
    if not force:
        existing = _find_existing_period_backup(target_dir, period, now)
        if existing:
            if period == 'weekly':
                iso_y, iso_w = now.isocalendar()[:2]
                reason = f'Резервная копия за неделю {iso_y}-W{iso_w:02d} уже существует'
            else:
                reason = f'Резервная копия за {now.strftime("%Y-%m")} уже существует'
            return {
                'created': False,
                'path': existing,
                'skipped_reason': reason,
                'removed': [],
            }

    init_db()
    file_bytes, filename = create_db_backup_bytes()
    dst_path = os.path.join(target_dir, filename)
    tmp_path = dst_path + '.tmp'
    with open(tmp_path, 'wb') as f:
        f.write(file_bytes)
    os.replace(tmp_path, dst_path)

    removed = _prune_old_backups(target_dir, keep)
    period_label = 'еженедельная' if period == 'weekly' else 'ежемесячная'
    log_audit_event(
        username='AutoBackup',
        farm_name='System',
        action='BACKUP',
        entity_type='SETTINGS',
        details=f'Автоматическая {period_label} резервная копия: {filename}',
    )
    return {
        'created': True,
        'path': dst_path,
        'skipped_reason': None,
        'removed': removed,
    }


def _acquire_file_lock(lock_path, stale_sec=120):
    """Атомарный межпроцессный lock-файл на создание автобэкапа."""
    now = time.time()
    try:
        if os.path.exists(lock_path) and (now - os.path.getmtime(lock_path) > stale_sec):
            os.remove(lock_path)
    except OSError:
        pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        return True
    except OSError:
        return False


def maybe_run_auto_backup(backup_dir=None, period='weekly', keep=52, force_check=False):
    """
    Встроенный триггер автобэкапа внутри веб-приложения (работает на бесплатном тарифе без Tasks/cron).
    Выполняет быструю проверку не чаще 1 раза в час на процесс и создаёт ровно 1 копию в неделю.
    Безопасен при многопоточности и нескольких воркерах WSGI; не пробрасывает исключения в HTTP-ответ.
    """
    target_dir = get_backup_dir(backup_dir)
    cache_key = (os.path.abspath(Config.DATABASE), target_dir, period)
    now_ts = time.time()

    if not force_check:
        last_ts = _last_check_by_target.get(cache_key, 0.0)
        if now_ts - last_ts < _AUTO_CHECK_INTERVAL_SEC:
            return None

    with _auto_backup_lock:
        if not force_check:
            last_ts = _last_check_by_target.get(cache_key, 0.0)
            if time.time() - last_ts < _AUTO_CHECK_INTERVAL_SEC:
                return None

        _last_check_by_target[cache_key] = time.time()
        try:
            now_dt = datetime.now()
            if _find_existing_period_backup(target_dir, period, now_dt):
                return None

            os.makedirs(target_dir, exist_ok=True)
            lock_path = os.path.join(target_dir, '.auto_backup.lock')
            if not _acquire_file_lock(lock_path):
                return None
            try:
                return run_scheduled_backup(backup_dir=target_dir, period=period, keep=keep)
            finally:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
        except Exception:
            logger.exception("Ошибка встроенного автоматического резервного копирования")
            return None


def main():
    parser = argparse.ArgumentParser(description='Автоматическое резервное копирование БД SQLite')
    parser.add_argument('--dir', default=None, help='Каталог для сохранения копий (по умолчанию ./backups)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--weekly', action='store_true', help='Создавать не чаще одного раза в неделю (по умолчанию)')
    group.add_argument('--monthly', action='store_true', help='Создавать не чаще одного раза в месяц')
    parser.add_argument('--force', action='store_true', help='Создать копию принудительно, игнорируя проверку периода')
    parser.add_argument('--keep', type=int, default=52, help='Сколько последних копий хранить (по умолчанию 52 — 1 год при еженедельном запуске)')
    args = parser.parse_args()

    period = 'monthly' if args.monthly else 'weekly'
    res = run_scheduled_backup(
        backup_dir=args.dir,
        period=period,
        keep=args.keep,
        force=args.force,
    )
    if res['created']:
        print(f"OK: создана резервная копия {res['path']}")
        if res['removed']:
            print(f"Удалены старые копии: {', '.join(res['removed'])}")
    else:
        print(f"SKIP: {res['skipped_reason']} ({res['path']})")


if __name__ == '__main__':
    main()

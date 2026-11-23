# scheduled_backup.py - Автоматическое плановое резервное копирование базы данных SQLite
import argparse
import os
import re
import sys
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from backup import create_db_backup_bytes
from config import Config
from database import init_db, log_audit_event

_BACKUP_DATE_RE = re.compile(r'^agro_backup_RB_(\d{8})_\d{6}\.sqlite$')


def _find_existing_period_backup(backup_dir, period, now):
    """Ищет уже созданный бэкап за текущую неделю (ISO) или текущий месяц в папке backups."""
    if not os.path.isdir(backup_dir):
        return None
    target_week = now.isocalendar()[:2]
    target_month = (now.year, now.month)

    for fname in sorted(os.listdir(backup_dir)):
        m = _BACKUP_DATE_RE.match(fname)
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
        if fname.startswith('agro_backup_RB_') and fname.endswith('.sqlite')
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


def run_scheduled_backup(backup_dir=None, period='weekly', monthly=None, keep=12, force=False):
    """
    Создаёт резервную копию БД в папке backup_dir.
    По умолчанию period='weekly' — создаёт не более одной копии в календарную неделю (ISO),
    что позволяет безопасно запускать скрипт через ежедневный планировщик PythonAnywhere.
    """
    if monthly is True:
        period = 'monthly'
    elif monthly is False and period not in ('weekly', 'monthly'):
        period = 'weekly'

    target_dir = os.path.abspath(backup_dir or os.path.join(Config.BASE_DIR, 'backups'))
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


def main():
    parser = argparse.ArgumentParser(description='Автоматическое резервное копирование БД SQLite')
    parser.add_argument('--dir', default=None, help='Каталог для сохранения копий (по умолчанию ./backups)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--weekly', action='store_true', help='Создавать не чаще одного раза в неделю (по умолчанию)')
    group.add_argument('--monthly', action='store_true', help='Создавать не чаще одного раза в месяц')
    parser.add_argument('--force', action='store_true', help='Создать копию принудительно, игнорируя проверку периода')
    parser.add_argument('--keep', type=int, default=12, help='Сколько последних копий хранить (по умолчанию 12)')
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

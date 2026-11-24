# routes/admin.py - Системное администрирование, журнал аудита и резервные копии
import logging
import os
from flask import (
    Blueprint, render_template, request, redirect, 
    url_for, flash, session, jsonify, Response, abort, current_app
)
from config import Config
from database import get_db_connection, log_audit_event, get_all_settings, invalidate_settings_cache, run_write_transaction
from backup import create_db_backup_bytes, restore_db_from_upload
from scheduled_backup import get_backup_dir, is_valid_backup_filename, list_saved_backups, maybe_run_auto_backup
from .auth import login_required, admin_required

logger = logging.getLogger('cattle_app')
admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/reasons', methods=['GET', 'POST'])
@admin_required
def manage_reasons():
    """Управление справочником причин выбытия скота (для администратора)"""
    if request.method == 'POST':
        category = request.form.get('category')
        name = request.form.get('name', '').strip()

        if category and name:
            def _tx_add_reason(_conn, cursor):
                cursor.execute('INSERT OR IGNORE INTO disposal_reasons (category, name, is_active) VALUES (?, ?, 1)', (category, name))
                log_audit_event(
                    username=session.get('username'),
                    farm_name='Admin',
                    action='CREATE_REASON',
                    entity_type='REASON',
                    details=f"Добавлена новая причина '{name}' в категорию '{category}'",
                    user_id=session.get('user_id'),
                    ip_address=request.remote_addr,
                    cursor=cursor
                )
            run_write_transaction(_tx_add_reason)
            flash(f"Причина '{name}' успешно добавлена в справочник", 'success')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM disposal_reasons ORDER BY category, name')
    reasons = [dict(r) for r in cursor.fetchall()]
    conn.close()

    return render_template('manage_reasons.html', reasons=reasons)

@admin_bp.route('/reasons/toggle/<int:reason_id>', methods=['POST'])
@admin_required
def toggle_reason(reason_id):
    """Переключение активности причины выбытия"""
    def _tx_toggle(_conn, cursor):
        cursor.execute('SELECT * FROM disposal_reasons WHERE id = ?', (reason_id,))
        reason = cursor.fetchone()
        if not reason:
            return None
        new_status = 0 if reason['is_active'] == 1 else 1
        cursor.execute('UPDATE disposal_reasons SET is_active = ? WHERE id = ?', (new_status, reason_id))
        return reason['name'], new_status

    res = run_write_transaction(_tx_toggle)
    if res:
        name, new_status = res
        status_text = 'активирована' if new_status == 1 else 'скрыта'
        flash(f"Причина '{name}' {status_text}", 'info')
    return redirect(url_for('admin.manage_reasons'))

@admin_bp.route('/reasons/delete/<int:reason_id>', methods=['POST'])
@admin_required
def delete_reason(reason_id):
    """Удаление причины выбытия из справочника"""
    def _tx_del_reason(_conn, cursor):
        cursor.execute('SELECT * FROM disposal_reasons WHERE id = ?', (reason_id,))
        reason = cursor.fetchone()
        if not reason:
            return None
        cursor.execute('DELETE FROM disposal_reasons WHERE id = ?', (reason_id,))
        log_audit_event(
            username=session.get('username'),
            farm_name='Admin',
            action='DELETE_REASON',
            entity_type='REASON',
            details=f"Удалена причина '{reason['name']}' из категории '{reason['category']}'",
            user_id=session.get('user_id'),
            ip_address=request.remote_addr,
            cursor=cursor
        )
        return reason['name']

    deleted_name = run_write_transaction(_tx_del_reason)
    if deleted_name:
        flash(f"Причина '{deleted_name}' удалена из справочника", 'success')
    return redirect(url_for('admin.manage_reasons'))

@admin_bp.route('/audit_log')
@admin_required
def audit_log():
    """Журнал аудита действий пользователей с пагинацией"""
    page = max(1, request.args.get('page', 1, type=int))
    per_page = 100
    offset = (page - 1) * per_page
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) as cnt FROM audit_logs')
    total_logs = cursor.fetchone()['cnt']
    total_pages = max(1, (total_logs + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page
    cursor.execute('SELECT * FROM audit_logs ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?', (per_page, offset))
    logs = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return render_template('audit_log.html', logs=logs, page=page, total_pages=total_pages, total_logs=total_logs)

@admin_bp.route('/backup')
@admin_required
def backup():
    """Панель управления резервными копиями базы данных"""
    if not current_app.config.get('TESTING') or os.environ.get('ENABLE_AUTO_BACKUP_IN_TESTS'):
        maybe_run_auto_backup(force_check=True)
    auto_backups = list_saved_backups()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM audit_logs WHERE action = 'BACKUP' ORDER BY created_at DESC LIMIT 20")
    backup_logs = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return render_template('backup.html', backup_logs=backup_logs, auto_backups=auto_backups)

@admin_bp.route('/backup/archive/<filename>')
@admin_required
def download_archived_backup(filename):
    """Безопасное скачивание сохранённой еженедельной резервной копии из папки backups"""
    if not is_valid_backup_filename(filename):
        abort(404)
    backup_path = os.path.join(get_backup_dir(), filename)
    if not os.path.isfile(backup_path):
        abort(404)
    with open(backup_path, 'rb') as f:
        data = f.read()
    return Response(
        data,
        status=200,
        mimetype='application/octet-stream',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length': str(len(data)),
            'Cache-Control': 'no-store',
        }
    )

@admin_bp.route('/backup/download')
@admin_required
def download_backup():
    """Скачивание бинарного дампа SQLite базы данных"""
    try:
        file_bytes, filename = create_db_backup_bytes()
    except Exception as exc:
        logger.exception("Ошибка создания резервной копии базы данных")
        flash(f'Ошибка при создании резервной копии базы данных: {exc}', 'danger')
        return redirect(url_for('admin.backup'))

    log_audit_event(
        username=session.get('username'),
        farm_name='Admin',
        action='BACKUP',
        entity_type='SETTINGS',
        details=f"Скачана резервная копия базы данных: {filename}",
        user_id=session.get('user_id'),
        ip_address=request.remote_addr
    )
    return Response(
        file_bytes,
        status=200,
        mimetype='application/octet-stream',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Length': str(len(file_bytes)),
            'Cache-Control': 'no-store',
        }
    )

@admin_bp.route('/backup/restore', methods=['GET', 'POST'])
@admin_required
def restore_backup():
    """Восстановление базы данных из загруженного файла бэкапа"""
    if request.method == 'GET':
        return redirect(url_for('admin.backup'))

    if 'backup_file' not in request.files:
        flash('Файл резервной копии не выбран', 'danger')
        return redirect(url_for('admin.backup'))
        
    file = request.files['backup_file']
    if not file or not file.filename:
        flash('Файл резервной копии не выбран', 'danger')
        return redirect(url_for('admin.backup'))

    try:
        success, msg = restore_db_from_upload(file, session.get('username'), request.remote_addr)
    except Exception as exc:
        logger.exception("Непредвиденная ошибка при восстановлении базы данных")
        success, msg = False, f'Ошибка при восстановлении базы данных: {exc}'

    if success:
        flash(msg, 'success')
    else:
        flash(msg, 'danger')
    return redirect(url_for('admin.backup'))

@admin_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    """Просмотр и редактирование зоотехнических нормативов, порогов тревог, коэффициентов и реквизитов предприятия"""
    import math

    sc = Config.DEFAULT_SCORECARD_THRESHOLDS
    an = Config.DEFAULT_ANALYTICS_PARAMS

    numeric_fields = [
        'norm_fresh_cow_disposal', 'warn_fresh_cow_disposal', 'crit_fresh_cow_disposal',
        'norm_first_lactation_culling', 'warn_first_lactation_culling', 'crit_first_lactation_culling',
        'norm_mortality_rate', 'warn_mortality_rate', 'crit_mortality_rate',
        'norm_locomotor_disorders', 'warn_locomotor_disorders', 'crit_locomotor_disorders',
        'norm_mastitis_disorders', 'warn_mastitis_disorders', 'crit_mastitis_disorders',
        'high_yield_threshold_kg', 'matrix_warn_pct', 'matrix_crit_pct',
        'alert_death_growth_ratio', 'alert_cluster_min_count'
    ]
    text_fields = ['farm_org_name', 'farm_unp']

    current_settings = get_all_settings()

    if request.method == 'POST':
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        if session.get('user_type') != 'admin':
            if is_ajax:
                return jsonify({'status': 'error', 'message': 'Редактирование доступно только администраторам'}), 403
            flash('Редактирование доступно только администраторам', 'danger')
            return redirect(url_for('admin.settings'))

        req_data = request.get_json(silent=True)
        if req_data is None:
            req_data = request.form

        parsed_updates = {}
        invalid_fields = []
        for f in numeric_fields:
            val = req_data.get(f)
            if val is not None and str(val).strip() != '':
                raw = str(val).strip().replace('\u00a0', '').replace(' ', '').replace(',', '.')
                try:
                    num = float(raw)
                    if not math.isfinite(num) or num < 0:
                        invalid_fields.append(f)
                    else:
                        parsed_updates[f] = str(round(num, 4))
                except (ValueError, TypeError):
                    invalid_fields.append(f)

        for tf in text_fields:
            val = req_data.get(tf)
            if val is not None and str(val).strip() != '':
                cleaned = str(val).strip()[:160]
                parsed_updates[tf] = cleaned

        if invalid_fields:
            msg = 'Некорректное числовое значение в одном из полей нормативов.'
            if is_ajax:
                return jsonify({'status': 'error', 'message': msg, 'fields': invalid_fields}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin.settings'))

        if not parsed_updates:
            msg = 'Не передано ни одного параметра для сохранения.'
            if is_ajax:
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'warning')
            return redirect(url_for('admin.settings'))

        def _merged_float(key, default_val):
            raw_v = parsed_updates.get(key, current_settings.get(key, default_val))
            try:
                return float(raw_v)
            except (ValueError, TypeError):
                return float(default_val)

        threshold_triples = [
            ('Выбытие новотельных (0–30 DIM)', 'norm_fresh_cow_disposal', 'warn_fresh_cow_disposal', 'crit_fresh_cow_disposal'),
            ('Выбраковка первотёлок', 'norm_first_lactation_culling', 'warn_first_lactation_culling', 'crit_first_lactation_culling'),
            ('Общая летальность (Падёж)', 'norm_mortality_rate', 'warn_mortality_rate', 'crit_mortality_rate'),
            ('Болезни конечностей', 'norm_locomotor_disorders', 'warn_locomotor_disorders', 'crit_locomotor_disorders'),
            ('Маститы и патологии вымени', 'norm_mastitis_disorders', 'warn_mastitis_disorders', 'crit_mastitis_disorders'),
        ]
        for label, k_norm, k_warn, k_crit in threshold_triples:
            v_norm = _merged_float(k_norm, sc[k_norm])
            v_warn = _merged_float(k_warn, sc[k_warn])
            v_crit = _merged_float(k_crit, sc[k_crit])
            if not (v_norm <= v_warn <= v_crit):
                msg = f'Ошибка порогов «{label}»: должно выполняться Норма ≤ Внимание ≤ Критично ({v_norm} ≤ {v_warn} ≤ {v_crit}).'
                if is_ajax:
                    return jsonify({'status': 'error', 'message': msg, 'fields': [k_norm, k_warn, k_crit]}), 400
                flash(msg, 'danger')
                return redirect(url_for('admin.settings'))

        m_warn = _merged_float('matrix_warn_pct', an['matrix_warn_pct'])
        m_crit = _merged_float('matrix_crit_pct', an['matrix_crit_pct'])
        if m_warn > m_crit:
            msg = f'Порог внимания матрицы ферм ({m_warn}%) не может превышать критический порог ({m_crit}%).'
            if is_ajax:
                return jsonify({'status': 'error', 'message': msg, 'fields': ['matrix_warn_pct', 'matrix_crit_pct']}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin.settings'))

        def _tx_save_settings(_conn, cursor):
            for k, v in parsed_updates.items():
                cursor.execute('''
                    INSERT INTO system_settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                ''', (k, v))
            log_audit_event(
                username=session.get('username'),
                farm_name='Admin',
                action='UPDATE_SETTINGS',
                entity_type='SETTINGS',
                details=f"Обновлены зоотехнические нормативы и настройки ({len(parsed_updates)} параметров)",
                user_id=session.get('user_id'),
                ip_address=request.remote_addr,
                cursor=cursor
            )

        run_write_transaction(_tx_save_settings)
        invalidate_settings_cache()

        if is_ajax:
            return jsonify({'status': 'success', 'message': 'Нормативы, пороги тревог и параметры успешно сохранены!', 'updated': parsed_updates})

        flash('Зоотехнические нормативы, пороги и настройки предприятия успешно обновлены!', 'success')
        return redirect(url_for('admin.settings'))

    def _f(k, default):
        try:
            return float(current_settings.get(k, default))
        except (ValueError, TypeError):
            return float(default)

    settings_data = {
        'norm_fresh_cow_disposal': _f('norm_fresh_cow_disposal', sc['norm_fresh_cow_disposal']),
        'warn_fresh_cow_disposal': _f('warn_fresh_cow_disposal', sc['warn_fresh_cow_disposal']),
        'crit_fresh_cow_disposal': _f('crit_fresh_cow_disposal', sc['crit_fresh_cow_disposal']),
        'norm_first_lactation_culling': _f('norm_first_lactation_culling', sc['norm_first_lactation_culling']),
        'warn_first_lactation_culling': _f('warn_first_lactation_culling', sc['warn_first_lactation_culling']),
        'crit_first_lactation_culling': _f('crit_first_lactation_culling', sc['crit_first_lactation_culling']),
        'norm_mortality_rate': _f('norm_mortality_rate', sc['norm_mortality_rate']),
        'warn_mortality_rate': _f('warn_mortality_rate', sc['warn_mortality_rate']),
        'crit_mortality_rate': _f('crit_mortality_rate', sc['crit_mortality_rate']),
        'norm_locomotor_disorders': _f('norm_locomotor_disorders', sc['norm_locomotor_disorders']),
        'warn_locomotor_disorders': _f('warn_locomotor_disorders', sc['warn_locomotor_disorders']),
        'crit_locomotor_disorders': _f('crit_locomotor_disorders', sc['crit_locomotor_disorders']),
        'norm_mastitis_disorders': _f('norm_mastitis_disorders', sc['norm_mastitis_disorders']),
        'warn_mastitis_disorders': _f('warn_mastitis_disorders', sc['warn_mastitis_disorders']),
        'crit_mastitis_disorders': _f('crit_mastitis_disorders', sc['crit_mastitis_disorders']),
        'high_yield_threshold_kg': int(_f('high_yield_threshold_kg', an['high_yield_threshold_kg'])),
        'matrix_warn_pct': _f('matrix_warn_pct', an['matrix_warn_pct']),
        'matrix_crit_pct': _f('matrix_crit_pct', an['matrix_crit_pct']),
        'alert_death_growth_ratio': _f('alert_death_growth_ratio', an['alert_death_growth_ratio']),
        'alert_cluster_min_count': int(_f('alert_cluster_min_count', an['alert_cluster_min_count'])),
        'farm_org_name': current_settings.get('farm_org_name', Config.DEFAULT_ORG_NAME),
        'farm_unp': current_settings.get('farm_unp', Config.DEFAULT_UNP)
    }

    return render_template('settings.html', settings=settings_data, user_type=session.get('user_type'))

# routes/admin.py - Системное администрирование, журнал аудита и резервные копии
import io
from flask import (
    Blueprint, render_template, request, redirect, 
    url_for, flash, session, send_file, jsonify
)
from config import Config
from database import get_db_connection, log_audit_event, get_all_settings, invalidate_settings_cache
from backup import create_db_backup_bytes, restore_db_from_upload
from .auth import login_required, admin_required

admin_bp = Blueprint('admin', __name__)

@admin_bp.route('/reasons', methods=['GET', 'POST'])
@admin_required
def manage_reasons():
    """Управление справочником причин выбытия скота (для администратора)"""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if request.method == 'POST':
        category = request.form.get('category')
        name = request.form.get('name', '').strip()
        
        if category and name:
            cursor.execute('INSERT OR IGNORE INTO disposal_reasons (category, name, is_active) VALUES (?, ?, 1)', (category, name))
            conn.commit()
            
            log_audit_event(
                username=session.get('username'),
                farm_name='Admin',
                action='CREATE_REASON',
                entity_type='REASON',
                details=f"Добавлена новая причина '{name}' в категорию '{category}'",
                user_id=session.get('user_id'),
                ip_address=request.remote_addr
            )
            flash(f"Причина '{name}' успешно добавлена в справочник", 'success')
            
    cursor.execute('SELECT * FROM disposal_reasons ORDER BY category, name')
    reasons = [dict(r) for r in cursor.fetchall()]
    conn.close()
    
    return render_template('manage_reasons.html', reasons=reasons)

@admin_bp.route('/reasons/toggle/<int:reason_id>', methods=['POST'])
@admin_required
def toggle_reason(reason_id):
    """Переключение активности причины выбытия"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM disposal_reasons WHERE id = ?', (reason_id,))
    reason = cursor.fetchone()
    if reason:
        new_status = 0 if reason['is_active'] == 1 else 1
        cursor.execute('UPDATE disposal_reasons SET is_active = ? WHERE id = ?', (new_status, reason_id))
        conn.commit()
        status_text = 'активирована' if new_status == 1 else 'скрыта'
        flash(f"Причина '{reason['name']}' {status_text}", 'info')
    conn.close()
    return redirect(url_for('admin.manage_reasons'))

@admin_bp.route('/reasons/delete/<int:reason_id>', methods=['POST'])
@admin_required
def delete_reason(reason_id):
    """Удаление причины выбытия из справочника"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM disposal_reasons WHERE id = ?', (reason_id,))
    reason = cursor.fetchone()
    if reason:
        cursor.execute('DELETE FROM disposal_reasons WHERE id = ?', (reason_id,))
        conn.commit()
        log_audit_event(
            username=session.get('username'),
            farm_name='Admin',
            action='DELETE_REASON',
            entity_type='REASON',
            details=f"Удалена причина '{reason['name']}' из категории '{reason['category']}'",
            user_id=session.get('user_id'),
            ip_address=request.remote_addr
        )
        flash(f"Причина '{reason['name']}' удалена из справочника", 'success')
    conn.close()
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
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM audit_logs WHERE action = 'BACKUP' ORDER BY created_at DESC LIMIT 20")
    backup_logs = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return render_template('backup.html', backup_logs=backup_logs)

@admin_bp.route('/backup/download')
@admin_required
def download_backup():
    """Скачивание бинарного дампа SQLite базы данных"""
    file_bytes, filename = create_db_backup_bytes()
    log_audit_event(
        username=session.get('username'),
        farm_name='Admin',
        action='BACKUP',
        entity_type='SETTINGS',
        details=f"Скачана резервная копия базы данных: {filename}",
        user_id=session.get('user_id'),
        ip_address=request.remote_addr
    )
    return send_file(
        io.BytesIO(file_bytes),
        download_name=filename,
        as_attachment=True,
        mimetype='application/octet-stream'
    )

@admin_bp.route('/backup/restore', methods=['POST'])
@admin_required
def restore_backup():
    """Восстановление базы данных из загруженного файла бэкапа"""
    if 'backup_file' not in request.files:
        flash('Файл резервной копии не выбран', 'danger')
        return redirect(url_for('admin.backup'))
        
    file = request.files['backup_file']
    if file.filename == '':
        flash('Файл резервной копии не выбран', 'danger')
        return redirect(url_for('admin.backup'))
        
    success, msg = restore_db_from_upload(file, session.get('username'), request.remote_addr)
    if success:
        flash(msg, 'success')
    else:
        flash(msg, 'danger')
    return redirect(url_for('admin.backup'))

@admin_bp.route('/seed_test_data')
@admin_required
def seed_test_data():
    """Генерация реалистичного массива тестовых данных (1 000 коров) — только для администраторов"""
    from add_test_cows import generate_test_cows
    generate_test_cows(1000)
    log_audit_event(
        username=session.get('username'),
        farm_name='Admin',
        action='SEED_TEST_DATA',
        entity_type='SETTINGS',
        details="Сгенерировано 1 000 тестовых записей выбытия КРС",
        user_id=session.get('user_id'),
        ip_address=request.remote_addr
    )
    flash('Успешно добавлено 1 000 коров в базу данных!', 'success')
    return redirect(url_for('cows.view_cows'))

@admin_bp.route('/pricing', methods=['GET', 'POST'])
@admin_required
def pricing():
    """Просмотр и редактирование нормативов цен, порогов тревог, аналитических коэффициентов и реквизитов предприятия"""
    import math

    dp = Config.DEFAULT_PRICES
    sc = Config.DEFAULT_SCORECARD_THRESHOLDS
    an = Config.DEFAULT_ANALYTICS_PARAMS

    numeric_fields = [
        'price_calves', 'price_youngstock', 'price_heifers', 'price_cows', 'price_dry_cows',
        'meat_price_per_kg', 'milk_price_per_kg', 'replacement_cost',
        'norm_fresh_cow_disposal', 'warn_fresh_cow_disposal', 'crit_fresh_cow_disposal',
        'norm_first_lactation_culling', 'warn_first_lactation_culling', 'crit_first_lactation_culling',
        'norm_mortality_rate', 'warn_mortality_rate', 'crit_mortality_rate',
        'norm_locomotor_disorders', 'warn_locomotor_disorders', 'crit_locomotor_disorders',
        'norm_mastitis_disorders', 'warn_mastitis_disorders', 'crit_mastitis_disorders',
        'sanitary_meat_factor', 'standard_lactation_days', 'default_daily_milk_kg',
        'default_weight_cow', 'default_weight_youngstock', 'default_weight_calf',
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
            return redirect(url_for('admin.pricing'))

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
            return redirect(url_for('admin.pricing'))

        if not parsed_updates:
            msg = 'Не передано ни одного параметра для сохранения.'
            if is_ajax:
                return jsonify({'status': 'error', 'message': msg}), 400
            flash(msg, 'warning')
            return redirect(url_for('admin.pricing'))

        def _merged_float(key, default_val):
            raw_v = parsed_updates.get(key, current_settings.get(key, default_val))
            try:
                return float(raw_v)
            except (ValueError, TypeError):
                return float(default_val)

        # Проверка логических ограничений (0<=санфактор<=1, Порог Норма <= Внимание <= Критично)
        san_factor = _merged_float('sanitary_meat_factor', an['sanitary_meat_factor'])
        if san_factor < 0.0 or san_factor > 1.0:
            msg = 'Коэффициент санприрезки должен быть в диапазоне от 0.00 до 1.00 (0%–100%).'
            if is_ajax:
                return jsonify({'status': 'error', 'message': msg, 'fields': ['sanitary_meat_factor']}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin.pricing'))

        if _merged_float('standard_lactation_days', an['standard_lactation_days']) < 1:
            msg = 'Стандартная продолжительность лактации должна быть больше 0 дней.'
            if is_ajax:
                return jsonify({'status': 'error', 'message': msg, 'fields': ['standard_lactation_days']}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin.pricing'))

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
                return redirect(url_for('admin.pricing'))

        m_warn = _merged_float('matrix_warn_pct', an['matrix_warn_pct'])
        m_crit = _merged_float('matrix_crit_pct', an['matrix_crit_pct'])
        if m_warn > m_crit:
            msg = f'Порог внимания матрицы ферм ({m_warn}%) не может превышать критический порог ({m_crit}%).'
            if is_ajax:
                return jsonify({'status': 'error', 'message': msg, 'fields': ['matrix_warn_pct', 'matrix_crit_pct']}), 400
            flash(msg, 'danger')
            return redirect(url_for('admin.pricing'))

        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            for k, v in parsed_updates.items():
                cursor.execute('''
                    INSERT INTO system_settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                ''', (k, v))
            conn.commit()
        finally:
            conn.close()

        invalidate_settings_cache()

        log_audit_event(
            username=session.get('username'),
            farm_name='Admin',
            action='UPDATE_SETTINGS',
            entity_type='SETTINGS',
            details=f"Обновлены нормативы и настройки ({len(parsed_updates)} параметров): мясо={parsed_updates.get('meat_price_per_kg', '-')}, молоко={parsed_updates.get('milk_price_per_kg', '-')}, ремонт={parsed_updates.get('replacement_cost', '-')}",
            user_id=session.get('user_id'),
            ip_address=request.remote_addr
        )

        if is_ajax:
            return jsonify({'status': 'success', 'message': 'Экономические нормативы и параметры расчётов успешно сохранены!', 'updated': parsed_updates})

        flash('Экономические нормативы, пороги и настройки предприятия успешно обновлены!', 'success')
        return redirect(url_for('admin.pricing'))

    def _f(k, default):
        try:
            return float(current_settings.get(k, default))
        except (ValueError, TypeError):
            return float(default)

    pricing_data = {
        'price_calves': _f('price_calves', dp['price_calves']),
        'price_youngstock': _f('price_youngstock', dp['price_youngstock']),
        'price_heifers': _f('price_heifers', dp['price_heifers']),
        'price_cows': _f('price_cows', dp['price_cows']),
        'price_dry_cows': _f('price_dry_cows', dp['price_dry_cows']),
        'meat_price_per_kg': _f('meat_price_per_kg', dp['meat_price_per_kg']),
        'milk_price_per_kg': _f('milk_price_per_kg', dp['milk_price_per_kg']),
        'replacement_cost': _f('replacement_cost', dp['replacement_cost']),
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
        'sanitary_meat_factor': _f('sanitary_meat_factor', an['sanitary_meat_factor']),
        'standard_lactation_days': int(_f('standard_lactation_days', an['standard_lactation_days'])),
        'default_daily_milk_kg': _f('default_daily_milk_kg', an['default_daily_milk_kg']),
        'default_weight_cow': _f('default_weight_cow', an['default_weight_cow']),
        'default_weight_youngstock': _f('default_weight_youngstock', an['default_weight_youngstock']),
        'default_weight_calf': _f('default_weight_calf', an['default_weight_calf']),
        'high_yield_threshold_kg': int(_f('high_yield_threshold_kg', an['high_yield_threshold_kg'])),
        'matrix_warn_pct': _f('matrix_warn_pct', an['matrix_warn_pct']),
        'matrix_crit_pct': _f('matrix_crit_pct', an['matrix_crit_pct']),
        'alert_death_growth_ratio': _f('alert_death_growth_ratio', an['alert_death_growth_ratio']),
        'alert_cluster_min_count': int(_f('alert_cluster_min_count', an['alert_cluster_min_count'])),
        'farm_org_name': current_settings.get('farm_org_name', Config.DEFAULT_ORG_NAME),
        'farm_unp': current_settings.get('farm_unp', Config.DEFAULT_UNP)
    }

    return render_template('pricing.html', pricing=pricing_data, user_type=session.get('user_type'))

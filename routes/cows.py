# routes/cows.py - Управление записями выбытия скота (Журнал коров)
import csv
import io
import json
from datetime import date, datetime
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, jsonify, send_file
)
import openpyxl

from config import Config
from database import get_db_connection, log_audit_event, run_write_transaction
from domain import (
    compute_lactation_phase,
    normalize_date,
    validate_cow_payload,
    validate_stillborn_payload,
)
from cow_queries import (
    build_age_group_sql,
    build_lactation_phase_sql,
    build_pathology_system_sql,
    build_cows_where_clause,
    insert_cow_record,
    insert_stillborn_record,
)
from .auth import login_required

cows_bp = Blueprint('cows', __name__)


@cows_bp.route('/cows')
@login_required
def view_cows():
    """Просмотр журнала выбытия коров (пагинированный, первые 50 строк в HTML)."""
    user_type = session.get('user_type')
    farm_name = session.get('farm_name')

    where_sql, params, meta = build_cows_where_clause(request.args, user_type, farm_name)

    conn = get_db_connection()
    cursor = conn.cursor()

    if user_type == 'admin':
        cursor.execute('''
            SELECT DISTINCT farm_name FROM (
                SELECT farm_name FROM users WHERE farm_name IS NOT NULL AND farm_name != ''
                UNION
                SELECT farm_name FROM cows WHERE farm_name IS NOT NULL AND farm_name != ''
                UNION
                SELECT farm_name FROM stillbirth_records WHERE farm_name IS NOT NULL AND farm_name != ''
            ) ORDER BY farm_name
        ''')
        farms = [r['farm_name'] for r in cursor.fetchall()]
    else:
        farms = [farm_name]

    cursor.execute(f'SELECT COUNT(*) as cnt FROM cows{where_sql}', params)
    total_count = cursor.fetchone()['cnt']
    cursor.execute(
        f'SELECT id, cow_id, ear_tag, farm_name, age_group, category, reason, '
        f'weight, lactation, lactation_days, lactation_phase, disposal_date, '
        f'is_pregnant, gestation_days, is_stillborn '
        f'FROM cows{where_sql} ORDER BY disposal_date DESC, id DESC LIMIT 50',
        params
    )
    cows = [dict(c) for c in cursor.fetchall()]
    conn.close()

    return render_template(
        'view_cows.html',
        cows=cows,
        user_type=user_type,
        total_count=total_count,
        farms=farms,
        categories=Config.DISPOSAL_CATEGORIES,
        age_groups=Config.AGE_GROUPS,
        breeds=Config.BREEDS,
        repro_filter=meta['repro_filter'],
        initial_farm=meta['farm'],
        initial_category=meta['category'],
        initial_reason=meta['reason'],
        initial_pathology_system=meta['pathology_system'],
        initial_age_group=meta['age_group'],
        initial_lactation_phase=meta['lactation_phase'],
        initial_start_date=meta['start_date'],
        initial_end_date=meta['end_date'],
        initial_period=meta['period'],
    )


@cows_bp.route('/api/cows')
@login_required
def api_cows():
    """Быстрый пагинированный API для журнала коров."""
    user_type = session.get('user_type')
    session_farm = session.get('farm_name')

    page = max(1, request.args.get('page', 1, type=int))
    limit = min(500, max(10, request.args.get('limit', 50, type=int)))
    sort = (request.args.get('sort') or 'newest').strip()

    where_clause, params, _ = build_cows_where_clause(request.args, user_type, session_farm)

    sort_map = {
        'oldest': 'ORDER BY disposal_date ASC, id ASC',
        'weight_desc': 'ORDER BY weight DESC',
        'id_asc': 'ORDER BY id ASC',
        'id_desc': 'ORDER BY id DESC',
    }
    order_clause = sort_map.get(sort, 'ORDER BY disposal_date DESC, id DESC')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f'SELECT COUNT(*) as cnt FROM cows{where_clause}', params)
    total_filtered = cursor.fetchone()['cnt']

    offset = (page - 1) * limit
    cursor.execute(
        f'SELECT id, cow_id, ear_tag, farm_name, age_group, category, reason, '
        f'weight, lactation, lactation_days, lactation_phase, disposal_date, '
        f'is_pregnant, gestation_days, is_stillborn '
        f'FROM cows{where_clause} {order_clause} LIMIT ? OFFSET ?',
        params + [limit, offset]
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    total_pages = max(1, (total_filtered + limit - 1) // limit)

    return jsonify({
        'success': True,
        'total_filtered': total_filtered,
        'page': page,
        'limit': limit,
        'total_pages': total_pages,
        'start_index': offset + 1 if total_filtered > 0 else 0,
        'end_index': min(offset + limit, total_filtered),
        'cows': rows
    })


@cows_bp.route('/add_cow', methods=['GET', 'POST'])
@login_required
def add_cow():
    """Добавление единичной записи выбытия КРС или акта мертворождения."""
    if session.get('user_type') == 'admin':
        flash('Администраторы не могут добавлять коров', 'warning')
        return redirect(url_for('main.dashboard'))

    farm_name = session.get('farm_name')
    today_dt = date.today()
    today = today_dt.strftime('%Y-%m-%d')

    def _render_add(form_data=None):
        return render_template(
            'add_cow.html',
            categories=Config.DISPOSAL_CATEGORIES,
            farm_name=farm_name,
            breeds=Config.BREEDS,
            default_breed=Config.DEFAULT_BREED,
            age_groups=Config.AGE_GROUPS,
            mother_groups=Config.MOTHER_GROUPS,
            event_types=Config.STILLBIRTH_EVENT_TYPES,
            fetus_sexes=Config.FETUS_SEXES,
            stillborn_causes=Config.STILLBIRTH_CAUSES,
            mother_conditions=Config.MOTHER_CONDITIONS,
            default_attendant=Config.DEFAULT_ATTENDANT,
            stillborn_prefill=Config.STILLBORN_PREFILL_MAP,
            today=today,
            form_data=form_data or {}
        )

    if request.method == 'POST':
        if request.form.get('form_mode') == 'stillborn':
            cleaned_sb, errors_sb = validate_stillborn_payload(request.form, today_date=today_dt)
            if errors_sb:
                flash(' '.join(errors_sb), 'danger')
                return _render_add(request.form)

            def _tx_add_sb(_conn, cursor):
                new_id, sb_code = insert_stillborn_record(cursor, farm_name, session.get('user_id'), cleaned_sb)
                log_audit_event(
                    username=session.get('username'),
                    farm_name=farm_name,
                    action='CREATE',
                    entity_type='STILLBORN',
                    entity_id=new_id,
                    details=f"Зарегистрирован акт мертворождения {sb_code} (мать: {cleaned_sb['mother_tag'] or '-'}, {cleaned_sb['mother_group']}, пол плода: {cleaned_sb['fetus_sex']})",
                    user_id=session.get('user_id'),
                    ip_address=request.remote_addr,
                    cursor=cursor
                )
                return sb_code

            sb_code = run_write_transaction(_tx_add_sb)
            flash(f'Акт мертворождения успешно зарегистрирован под номером: {sb_code}', 'success')
            return redirect(url_for('stillborn.stillborn_list'))

        cleaned, errors = validate_cow_payload(request.form, today_date=today_dt)
        if errors:
            flash(' '.join(errors), 'danger')
            return _render_add(request.form)

        def _tx_add_cow(_conn, cursor):
            new_id, cow_id = insert_cow_record(cursor, farm_name, session.get('user_id'), cleaned)
            log_audit_event(
                username=session.get('username'),
                farm_name=farm_name,
                action='CREATE',
                entity_type='COW',
                entity_id=new_id,
                details=f"Добавлена корова {cow_id} (Ушной № {cleaned['ear_tag'] or '-'}), {cleaned['category']}: {cleaned['reason']}, фаза: {cleaned['lactation_phase']}",
                user_id=session.get('user_id'),
                ip_address=request.remote_addr,
                cursor=cursor
            )
            return cow_id

        cow_id = run_write_transaction(_tx_add_cow)

        flash(f'Корова успешно добавлена с системным номером: {cow_id}', 'success')
        return redirect(url_for('cows.view_cows'))

    return _render_add()


@cows_bp.route('/edit_cow/<int:cow_id>', methods=['GET', 'POST'])
@login_required
def edit_cow(cow_id):
    """Редактирование записи о корове."""
    user_type = session.get('user_type')
    farm_name = session.get('farm_name')
    today_dt = date.today()
    today = today_dt.strftime('%Y-%m-%d')

    conn = get_db_connection()
    cursor = conn.cursor()

    if user_type == 'admin':
        cursor.execute('SELECT * FROM cows WHERE id = ?', (cow_id,))
    else:
        cursor.execute('SELECT * FROM cows WHERE id = ? AND farm_name = ?', (cow_id, farm_name))

    cow_row = cursor.fetchone()
    if not cow_row:
        conn.close()
        flash('Запись не найдена или у вас нет прав на её редактирование', 'danger')
        return redirect(url_for('cows.view_cows'))

    cow = dict(cow_row)

    if request.method == 'POST':
        cleaned, errors = validate_cow_payload(request.form, today_date=today_dt)
        if errors:
            flash(' '.join(errors), 'danger')
            cow.update({
                'category': request.form.get('category') or cow.get('category'),
                'reason': request.form.get('reason') or cow.get('reason'),
                'disposal_date': request.form.get('disposal_date') or cow.get('disposal_date'),
                'ear_tag': request.form.get('ear_tag'),
                'lactation': request.form.get('lactation'),
                'lactation_days': request.form.get('lactation_days'),
                'weight': request.form.get('weight'),
                'notes': request.form.get('notes'),
                'age_group': request.form.get('age_group') or cow.get('age_group'),
                'breed': request.form.get('breed') or cow.get('breed'),
                'milk_yield': request.form.get('milk_yield'),
                'is_pregnant': cleaned['is_pregnant'],
                'gestation_days': cleaned['gestation_days'],
            })
            conn.close()
            return render_template(
                'edit_cow.html',
                cow=cow,
                categories=Config.DISPOSAL_CATEGORIES,
                breeds=Config.BREEDS,
                age_groups=Config.AGE_GROUPS,
                today=today
            )

        conn.close()

        def _tx_edit(_conn, tx_cursor):
            tx_cursor.execute('''
                UPDATE cows SET
                    category = ?,
                    reason = ?,
                    disposal_date = ?,
                    ear_tag = ?,
                    lactation = ?,
                    lactation_days = ?,
                    lactation_phase = ?,
                    pathology_system = ?,
                    weight = ?,
                    notes = ?,
                    age_group = ?,
                    breed = ?,
                    milk_yield = ?,
                    is_pregnant = ?,
                    gestation_days = ?,
                    is_stillborn = 0,
                    updated_at = CURRENT_TIMESTAMP,
                    updated_by = ?
                WHERE id = ?
            ''', (
                cleaned['category'], cleaned['reason'], cleaned['disposal_date'], cleaned['ear_tag'],
                cleaned['lactation'], cleaned['lactation_days'], cleaned['lactation_phase'], cleaned['pathology_system'],
                cleaned['weight'], cleaned['notes'], cleaned['age_group'], cleaned['breed'],
                cleaned['milk_yield'], cleaned['is_pregnant'],
                cleaned['gestation_days'], session.get('user_id'), cow_id
            ))
            log_audit_event(
                username=session.get('username'),
                farm_name=cow['farm_name'],
                action='UPDATE',
                entity_type='COW',
                entity_id=cow_id,
                details=f"Отредактирована запись {cow['cow_id']}. Новые данные: {cleaned['category']} - {cleaned['reason']}, дата {cleaned['disposal_date']}",
                user_id=session.get('user_id'),
                ip_address=request.remote_addr,
                cursor=tx_cursor
            )

        run_write_transaction(_tx_edit)

        flash(f'Запись {cow["cow_id"]} успешно обновлена', 'success')
        return redirect(url_for('cows.view_cows'))

    conn.close()
    return render_template(
        'edit_cow.html',
        cow=cow,
        categories=Config.DISPOSAL_CATEGORIES,
        breeds=Config.BREEDS,
        age_groups=Config.AGE_GROUPS,
        today=today
    )


@cows_bp.route('/delete_cow/<int:cow_id>', methods=['POST'])
@login_required
def delete_cow(cow_id):
    """Удаление записи выбытия."""
    user_type = session.get('user_type')
    farm_name = session.get('farm_name')

    def _tx_delete(_conn, cursor):
        if user_type == 'admin':
            cursor.execute('SELECT * FROM cows WHERE id = ?', (cow_id,))
        else:
            cursor.execute('SELECT * FROM cows WHERE id = ? AND farm_name = ?', (cow_id, farm_name))
        cow = cursor.fetchone()
        if not cow:
            return None
        cow_dict = dict(cow)
        cursor.execute('DELETE FROM cows WHERE id = ?', (cow_id,))
        log_audit_event(
            username=session.get('username'),
            farm_name=cow_dict['farm_name'],
            action='DELETE',
            entity_type='COW',
            entity_id=cow_id,
            details=f"Удалена запись {cow_dict['cow_id']} ({cow_dict['category']}: {cow_dict['reason']})",
            user_id=session.get('user_id'),
            ip_address=request.remote_addr,
            cursor=cursor
        )
        return cow_dict

    cow_dict = run_write_transaction(_tx_delete)
    if not cow_dict:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'Запись не найдена'}), 404
        flash('Запись не найдена', 'danger')
        return redirect(url_for('cows.view_cows'))

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'message': 'Запись удалена'})

    flash(f'Запись {cow_dict["cow_id"]} успешно удалена', 'success')
    return redirect(url_for('cows.view_cows'))


@cows_bp.route('/bulk_delete_cows', methods=['POST'])
@cows_bp.route('/delete_multiple_cows', methods=['POST'], endpoint='delete_multiple_cows')
@login_required
def bulk_delete_cows():
    """Массовое удаление записей."""
    data = request.get_json(silent=True) or {}
    raw_ids = data.get('ids', []) or data.get('cow_ids', [])
    cow_ids = []
    for item in raw_ids:
        try:
            cow_ids.append(int(item))
        except (ValueError, TypeError):
            continue

    if not cow_ids:
        return jsonify({'error': 'Не выбрано ни одной записи'}), 400

    user_type = session.get('user_type')
    farm_name = session.get('farm_name')

    def _tx_bulk_delete(_conn, cursor):
        placeholders = ','.join('?' * len(cow_ids))
        if user_type == 'admin':
            cursor.execute(f"SELECT id FROM cows WHERE id IN ({placeholders})", cow_ids)
        else:
            cursor.execute(f"SELECT id FROM cows WHERE id IN ({placeholders}) AND farm_name = ?", cow_ids + [farm_name])
        valid_ids = [c['id'] for c in cursor.fetchall()]
        if valid_ids:
            del_placeholders = ','.join('?' * len(valid_ids))
            cursor.execute(f"DELETE FROM cows WHERE id IN ({del_placeholders})", valid_ids)
            log_audit_event(
                username=session.get('username'),
                farm_name=farm_name or 'Admin',
                action='BULK_DELETE',
                entity_type='COW',
                details=f"Массовое удаление {len(valid_ids)} записей",
                user_id=session.get('user_id'),
                ip_address=request.remote_addr,
                cursor=cursor
            )
        return len(valid_ids)

    deleted_count = run_write_transaction(_tx_bulk_delete)
    return jsonify({'success': True, 'deleted_count': deleted_count})


@cows_bp.route('/add_multiple_cows', methods=['GET', 'POST'])
@login_required
def add_multiple_cows():
    """Множественное добавление коров табличным списком."""
    if session.get('user_type') == 'admin':
        if request.is_json:
            return jsonify({'error': 'Администраторы не могут добавлять коров'}), 403
        flash('Администраторы не могут добавлять коров', 'warning')
        return redirect(url_for('main.dashboard'))

    farm_name = session.get('farm_name')

    if request.method == 'POST':
        cows_data = None
        raw_text_input = ''
        if request.is_json:
            data = request.get_json(silent=True)
            if isinstance(data, list):
                cows_data = data
            elif isinstance(data, dict):
                cows_data = data.get('cows_data')
        else:
            raw_text_input = request.form.get('cows_data') or ''
            if raw_text_input.strip():
                try:
                    parsed = json.loads(raw_text_input)
                    cows_data = parsed if isinstance(parsed, list) else (parsed.get('cows_data') if isinstance(parsed, dict) else None)
                except Exception:
                    cows_data = None

        if not isinstance(cows_data, list) or len(cows_data) == 0:
            err_msg = 'Передайте корректный непустой JSON-массив записей.'
            if request.is_json:
                return jsonify({'success': False, 'error': err_msg, 'added_count': 0, 'skipped_count': 0, 'errors': []}), 400
            flash(err_msg, 'danger')
            return render_template('add_multiple_cows.html', categories=Config.DISPOSAL_CATEGORIES, farm_name=farm_name, raw_cows_data=raw_text_input), 400

        today_dt = date.today()
        today_str = today_dt.strftime('%Y-%m-%d')

        # Предварительная валидация всех строк ДО открытия пишущей транзакции
        prepared_ops = []
        row_errors = []
        for idx, item in enumerate(cows_data, start=1):
            if not isinstance(item, dict):
                row_errors.append({'row': idx, 'errors': ['Ожидается JSON-объект записи.']})
                continue
            form_mode = (item.get('form_mode') or '').strip().lower()
            is_stillborn_flag = item.get('is_stillborn') in [1, '1', True, 'true', 'on']
            item_copy = dict(item)
            if not item_copy.get('disposal_date') and not item_copy.get('event_date'):
                item_copy['disposal_date'] = today_str

            if form_mode == 'stillborn' or is_stillborn_flag:
                if not item_copy.get('reason') and not item_copy.get('event_type'):
                    item_copy['reason'] = Config.STILLBIRTH_EVENT_TYPES[0]
                cleaned_sb, errs_sb = validate_stillborn_payload(item_copy, today_date=today_dt)
                if errs_sb:
                    row_errors.append({'row': idx, 'errors': errs_sb})
                    continue
                prepared_ops.append(('stillborn', cleaned_sb))
                continue

            cleaned_cow, errs_cow = validate_cow_payload(item_copy, today_date=today_dt)
            if errs_cow:
                row_errors.append({'row': idx, 'errors': errs_cow})
                continue
            prepared_ops.append(('cow', cleaned_cow))

        added_cows_count = 0
        added_stillborn_count = 0
        if prepared_ops:
            def _tx_batch(_conn, cursor):
                cows_cnt = 0
                sb_cnt = 0
                for kind, payload in prepared_ops:
                    if kind == 'stillborn':
                        insert_stillborn_record(cursor, farm_name, session.get('user_id'), payload)
                        sb_cnt += 1
                    else:
                        insert_cow_record(cursor, farm_name, session.get('user_id'), payload)
                        cows_cnt += 1

                if cows_cnt + sb_cnt > 0:
                    log_audit_event(
                        username=session.get('username'),
                        farm_name=farm_name,
                        action='CREATE_BATCH',
                        entity_type='COW',
                        details=f"Пакетно добавлено: {cows_cnt} выбытий, {sb_cnt} мертворождений (пропущено с ошибками: {len(row_errors)})",
                        user_id=session.get('user_id'),
                        ip_address=request.remote_addr,
                        cursor=cursor
                    )
                return cows_cnt, sb_cnt

            added_cows_count, added_stillborn_count = run_write_transaction(_tx_batch)

        total_added = added_cows_count + added_stillborn_count
        skipped_count = len(row_errors)

        if request.is_json:
            payload = {
                'success': skipped_count == 0 and total_added > 0,
                'partial': skipped_count > 0 and total_added > 0,
                'added_count': total_added,
                'skipped_count': skipped_count,
                'cows_count': added_cows_count,
                'stillborn_count': added_stillborn_count,
                'errors': row_errors,
            }
            if skipped_count > 0:
                payload['message'] = (
                    f"Сохранено {total_added} из {len(cows_data)} записей. "
                    f"Отклонено строк с ошибками: {skipped_count}."
                )
            status_code = 200 if total_added > 0 else 400
            return jsonify(payload), status_code

        if skipped_count > 0:
            preview_errs = '; '.join(
                f"Строка {e['row']}: {' '.join(e['errors'])}" for e in row_errors[:5]
            )
            if skipped_count > 5:
                preview_errs += f" … и ещё {skipped_count - 5}"
            if total_added > 0:
                flash(
                    f'Частично добавлено записей: {total_added} (выбытие: {added_cows_count}, мертворождения: {added_stillborn_count}). '
                    f'Отклонено строк с ошибками ({skipped_count}): {preview_errs}',
                    'warning'
                )
                return redirect(url_for('cows.view_cows'))
            flash(f'Записи не добавлены. Ошибки в строках ({skipped_count}): {preview_errs}', 'danger')
            return render_template('add_multiple_cows.html', categories=Config.DISPOSAL_CATEGORIES, farm_name=farm_name, raw_cows_data=raw_text_input), 400

        flash(f'Успешно добавлено записей: {total_added} (выбытие: {added_cows_count}, мертворождения: {added_stillborn_count})', 'success')
        return redirect(url_for('cows.view_cows'))

    return render_template('add_multiple_cows.html', categories=Config.DISPOSAL_CATEGORIES, farm_name=farm_name, raw_cows_data='')


def _infer_age_group_from_import(rea_low, notes_low, lact):
    age_group = 'Полновозрастные коровы (2+ лакт.)'
    if lact == 1:
        age_group = 'Коровы 1-й лактации (первотёлки)'
    if 'тел' in rea_low or 'диспепс' in rea_low:
        age_group = 'Телята (до 6 мес.)'
    elif 'нетел' in rea_low:
        age_group = 'Нетели (>18 мес.)'
    elif 'сухосто' in rea_low:
        age_group = 'Сухостойные коровы'
    elif 'быч' in rea_low:
        age_group = 'Бычки на откорме'
    return age_group


def _is_explicit_stillborn_row(cat, rea_low):
    if cat in ('мертворождение', 'мертворождённые', 'аборт'):
        return True
    return rea_low.startswith(('мертворож', 'аборт'))


@cows_bp.route('/import_cows', methods=['GET', 'POST'])
@login_required
def import_cows():
    """Импорт данных выбытия из Excel / CSV файлов."""
    if session.get('user_type') == 'admin':
        flash('Только пользователи ферм могут импортировать данные своего хозяйства', 'warning')
        return redirect(url_for('main.dashboard'))

    farm_name = session.get('farm_name')
    today_dt = date.today()

    if request.method == 'POST':
        if 'file' not in request.files or not request.files['file'].filename:
            flash('Файл не выбран', 'danger')
            return redirect(request.url)

        file = request.files['file']
        filename = file.filename.lower()
        parsed_rows = []
        errors = []

        def _extract_row_fields(raw_cells):
            cat = str(raw_cells[0] or '').strip().lower()
            rea = str(raw_cells[1] or '').strip() if len(raw_cells) > 1 and raw_cells[1] is not None else ''
            d_date = normalize_date(raw_cells[2] if len(raw_cells) > 2 else '')
            ear_tag = str(raw_cells[3]).strip() if len(raw_cells) > 3 and raw_cells[3] not in (None, '') else None
            raw_lact = raw_cells[4] if len(raw_cells) > 4 else None
            raw_w = raw_cells[5] if len(raw_cells) > 5 else None
            notes = str(raw_cells[6]).strip() if len(raw_cells) > 6 and raw_cells[6] not in (None, '') else None
            return cat, rea, d_date, ear_tag, raw_lact, raw_w, notes

        try:
            if filename.endswith('.csv'):
                raw_text = file.stream.read().decode("utf-8-sig")
                stream = io.StringIO(raw_text, newline=None)
                csv_reader = csv.reader(stream, delimiter=';' if ';' in raw_text else ',')
                next(csv_reader, None)
                row_iter = enumerate(csv_reader, start=2)
            elif filename.endswith('.xlsx'):
                wb = openpyxl.load_workbook(file, data_only=True)
                ws = wb.active
                row_iter = enumerate(ws.iter_rows(min_row=2, values_only=True), start=2)
            elif filename.endswith('.xls'):
                flash('Старый бинарный формат .xls не поддерживается. Пересохраните файл в Excel как .xlsx или .csv.', 'danger')
                return redirect(request.url)
            else:
                flash('Поддерживаются только форматы .xlsx и .csv', 'danger')
                return redirect(request.url)

            for line_num, raw_cells in row_iter:
                if not raw_cells or len(raw_cells) < 3 or not raw_cells[0]:
                    continue
                cat, rea, d_date, ear_tag, raw_lact, raw_w, notes = _extract_row_fields(raw_cells)
                if cat not in ['падёж', 'выбраковка', 'санитарный', 'мертворождение', 'мертворождённые', 'аборт']:
                    errors.append(f"Строка {line_num}: недопустимая категория '{cat}'")
                    continue
                if not d_date:
                    errors.append(f"Строка {line_num}: некорректный формат даты")
                    continue
                parsed_rows.append((line_num, cat, rea, d_date, ear_tag, raw_lact, raw_w, notes))
        except Exception as e:
            flash(f'Ошибка обработки файла: {str(e)}', 'danger')
            return redirect(request.url)

        if parsed_rows:
            # Валидация до входа в пишущую транзакцию, чтобы минимизировать время удержания блокировки
            prepared_ops = []
            for line_num, cat, rea, d_date, ear_tag, raw_lact, raw_w, notes in parsed_rows:
                rea_low = (rea or '').lower()
                notes_low = (notes or '').lower()

                if _is_explicit_stillborn_row(cat, rea_low):
                    e_type = 'Аборт (1-й триместр, до 90 дн.)' if 'аборт' in rea_low or cat == 'аборт' else Config.STILLBIRTH_EVENT_TYPES[0]
                    cleaned_sb, errs_sb = validate_stillborn_payload({
                        'event_date': d_date,
                        'mother_tag': ear_tag,
                        'event_type': e_type,
                        'cause': rea or e_type,
                        'weight': raw_w,
                        'notes': notes,
                    }, today_date=today_dt)
                    if errs_sb:
                        errors.append(f"Строка {line_num}: {' '.join(errs_sb)}")
                        continue
                    prepared_ops.append(('stillborn', cleaned_sb))
                    continue

                lact_val = None
                if raw_lact is not None and str(raw_lact).strip().isdigit():
                    lact_val = int(str(raw_lact).strip())
                age_group = _infer_age_group_from_import(rea_low, notes_low, lact_val)
                is_preg = 1 if ('сухосто' in age_group.lower() or 'нетел' in age_group.lower() or 'стельн' in rea_low) else 0
                gest_days = (210 if 'сухосто' in age_group.lower() else 180) if is_preg else 0

                cleaned_cow, errs_cow = validate_cow_payload({
                    'category': cat,
                    'reason': rea,
                    'disposal_date': d_date,
                    'ear_tag': ear_tag,
                    'lactation': raw_lact,
                    'weight': raw_w,
                    'notes': notes,
                    'age_group': age_group,
                    'breed': Config.DEFAULT_BREED,
                    'is_pregnant': is_preg,
                    'gestation_days': gest_days,
                }, today_date=today_dt)
                if errs_cow:
                    errors.append(f"Строка {line_num}: {' '.join(errs_cow)}")
                    continue
                prepared_ops.append(('cow', cleaned_cow))

            inserted_cows = 0
            inserted_stillborn = 0
            if prepared_ops:
                def _tx_import(_conn, cursor):
                    c_cnt = 0
                    s_cnt = 0
                    for kind, payload in prepared_ops:
                        if kind == 'stillborn':
                            insert_stillborn_record(cursor, farm_name, session.get('user_id'), payload)
                            s_cnt += 1
                        else:
                            insert_cow_record(cursor, farm_name, session.get('user_id'), payload)
                            c_cnt += 1
                    if c_cnt or s_cnt:
                        log_audit_event(
                            username=session.get('username'),
                            farm_name=farm_name,
                            action='IMPORT',
                            entity_type='COW',
                            details=f"Импортировано из файла {file.filename}: {c_cnt} выбытий, {s_cnt} мертворождений/абортов",
                            user_id=session.get('user_id'),
                            ip_address=request.remote_addr,
                            cursor=cursor
                        )
                    return c_cnt, s_cnt

                inserted_cows, inserted_stillborn = run_write_transaction(_tx_import)

            if inserted_cows or inserted_stillborn:
                if errors:
                    preview = '; '.join(errors[:5])
                    if len(errors) > 5:
                        preview += f' … и ещё {len(errors) - 5}'
                    flash(
                        f'Частично импортировано записей: выбытие — {inserted_cows}, мертворождения/аборты — {inserted_stillborn}. '
                        f'Пропущено строк с ошибками ({len(errors)}): {preview}',
                        'warning'
                    )
                else:
                    flash(f'Успешно импортировано записей: выбытие — {inserted_cows}, мертворождения/аборты — {inserted_stillborn}!', 'success')
                return redirect(url_for('cows.view_cows'))

        flash('В файле не найдено корректных строк для импорта.', 'danger')
        if errors:
            flash(errors[0], 'warning')
        return redirect(request.url)

    return render_template('import_cows.html', farm_name=farm_name)


@cows_bp.route('/download_import_template')
@login_required
def download_import_template():
    """Скачать образец Excel-шаблона для импорта."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Шаблон_Импорта"

    headers = ['Категория (падёж/выбраковка/санитарный)*', 'Причина выбытия*', 'Дата (ГГГГ-ММ-ДД)*', 'Инв./Ушной №', 'Лактация', 'Масса (кг)', 'Примечание']
    ws.append(headers)

    today_s = datetime.now().strftime('%Y-%m-%d')
    ws.append(['падёж', 'Острая тимпания', today_s, '7701', 3, 520.5, 'Заключение ветврача'])
    ws.append(['выбраковка', 'Агалактия', today_s, '7702', 4, 480.0, 'Низкая продуктивность'])
    ws.append(['санитарный', 'Травма конечностей', today_s, '7703', 2, 510.0, 'Травма в секции'])

    for cell in ws[1]:
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
        cell.fill = openpyxl.styles.PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")
        cell.alignment = openpyxl.styles.Alignment(horizontal="center")

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return send_file(out, download_name="shablon_importa_korov.xlsx", as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

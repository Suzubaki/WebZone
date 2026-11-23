# routes/api.py - Внутреннее JSON API для динамических интерфейсов
from flask import Blueprint, jsonify, session
from database import get_db_connection, get_reasons_for_category
from .auth import login_required

api_bp = Blueprint('api', __name__)

@api_bp.route('/get_farms')
@login_required
def get_farms():
    """Получение списка доступных ферм для выпадающих списков фильтров."""
    if session.get('user_type') != 'admin':
        own_farm = (session.get('farm_name') or '').strip()
        return jsonify([own_farm] if own_farm else [])

    conn = get_db_connection()
    cursor = conn.cursor()
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
    conn.close()
    return jsonify(farms)

@api_bp.route('/get_reasons/<path:category>')
@login_required
def get_reasons(category):
    """Динамическая подгрузка активных причин выбытия для выбранной категории"""
    reasons = get_reasons_for_category(category)
    return jsonify(reasons)

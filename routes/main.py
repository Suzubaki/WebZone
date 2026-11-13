# routes/main.py - Главная страница и дашборд
import os
from flask import Blueprint, render_template, request, redirect, url_for, session, send_file, Response
from config import Config
from .auth import login_required
from charts import (
    get_anomaly_alerts, get_user_statistics, get_dashboard_full_data
)

main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
    """Главный входной маршрут - перенаправление на дашборд или логин"""
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))
    return render_template('index.html')

@main_bp.route('/dashboard')
@login_required
def dashboard():
    """Аналитический дашборд мониторинга выбытия КРС"""
    user_type = session.get('user_type')
    farm_name = session.get('farm_name')
    
    period = request.args.get('period', 'auto')
    custom_start = request.args.get('custom_start')
    custom_end = request.args.get('custom_end')
    
    selected_farm = farm_name
    if user_type == 'admin':
        selected_farm = request.args.get('farm', 'all')
        
    dashboard_data = get_dashboard_full_data(
        user_type=user_type,
        farm_name=selected_farm,
        period=period,
        custom_start=custom_start,
        custom_end=custom_end
    )
    alerts = get_anomaly_alerts(user_type, selected_farm)
    user_stats_data = get_user_statistics() if user_type == 'admin' else None
    
    from database import get_all_settings
    all_settings = get_all_settings()
    def _s(k, default):
        try:
            return float(all_settings.get(k, default))
        except (ValueError, TypeError):
            return float(default)

    pricing_data = {
        'price_calves': _s('price_calves', 900.0),
        'price_youngstock': _s('price_youngstock', 1650.0),
        'price_heifers': _s('price_heifers', 2700.0),
        'price_cows': _s('price_cows', 3200.0),
        'price_dry_cows': _s('price_dry_cows', 3000.0),
        'meat_price_per_kg': _s('meat_price_per_kg', 6.20),
        'milk_price_per_kg': _s('milk_price_per_kg', 1.18),
        'replacement_cost': _s('replacement_cost', 2950.0),
        'norm_fresh_cow_disposal': _s('norm_fresh_cow_disposal', 10.0),
        'warn_fresh_cow_disposal': _s('warn_fresh_cow_disposal', 14.0),
        'crit_fresh_cow_disposal': _s('crit_fresh_cow_disposal', 18.0),
        'norm_first_lactation_culling': _s('norm_first_lactation_culling', 20.0),
        'warn_first_lactation_culling': _s('warn_first_lactation_culling', 25.0),
        'crit_first_lactation_culling': _s('crit_first_lactation_culling', 30.0),
        'norm_mortality_rate': _s('norm_mortality_rate', 25.0),
        'warn_mortality_rate': _s('warn_mortality_rate', 35.0),
        'crit_mortality_rate': _s('crit_mortality_rate', 45.0),
        'norm_locomotor_disorders': _s('norm_locomotor_disorders', 15.0),
        'warn_locomotor_disorders': _s('warn_locomotor_disorders', 20.0),
        'crit_locomotor_disorders': _s('crit_locomotor_disorders', 25.0),
        'norm_mastitis_disorders': _s('norm_mastitis_disorders', 15.0),
        'warn_mastitis_disorders': _s('warn_mastitis_disorders', 20.0),
        'crit_mastitis_disorders': _s('crit_mastitis_disorders', 25.0)
    }
    
    return render_template('dashboard.html',
                           user_type=user_type,
                           farm_name=farm_name,
                           selected_farm=selected_farm,
                           d=dashboard_data,
                           alerts=alerts,
                           user_stats_data=user_stats_data,
                           pricing=pricing_data)

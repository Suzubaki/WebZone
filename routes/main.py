# routes/main.py - Главная страница и дашборд
from flask import Blueprint, render_template, request, redirect, url_for, session
from config import Config
from database import get_all_settings
from .auth import login_required
from charts import get_anomaly_alerts, get_dashboard_full_data

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
        selected_farm = (request.args.get('farm') or 'all').strip() or 'all'
        
    dashboard_data = get_dashboard_full_data(
        user_type=user_type,
        farm_name=selected_farm,
        period=period,
        custom_start=custom_start,
        custom_end=custom_end
    )
    alerts = get_anomaly_alerts(user_type, selected_farm)
    
    all_settings = get_all_settings()
    def _s(k, default):
        try:
            return float(all_settings.get(k, default))
        except (ValueError, TypeError):
            return float(default)

    sc = Config.DEFAULT_SCORECARD_THRESHOLDS
    an = Config.DEFAULT_ANALYTICS_PARAMS

    settings_data = {
        'norm_fresh_cow_disposal': _s('norm_fresh_cow_disposal', sc['norm_fresh_cow_disposal']),
        'warn_fresh_cow_disposal': _s('warn_fresh_cow_disposal', sc['warn_fresh_cow_disposal']),
        'crit_fresh_cow_disposal': _s('crit_fresh_cow_disposal', sc['crit_fresh_cow_disposal']),
        'norm_first_lactation_culling': _s('norm_first_lactation_culling', sc['norm_first_lactation_culling']),
        'warn_first_lactation_culling': _s('warn_first_lactation_culling', sc['warn_first_lactation_culling']),
        'crit_first_lactation_culling': _s('crit_first_lactation_culling', sc['crit_first_lactation_culling']),
        'norm_mortality_rate': _s('norm_mortality_rate', sc['norm_mortality_rate']),
        'warn_mortality_rate': _s('warn_mortality_rate', sc['warn_mortality_rate']),
        'crit_mortality_rate': _s('crit_mortality_rate', sc['crit_mortality_rate']),
        'norm_locomotor_disorders': _s('norm_locomotor_disorders', sc['norm_locomotor_disorders']),
        'warn_locomotor_disorders': _s('warn_locomotor_disorders', sc['warn_locomotor_disorders']),
        'crit_locomotor_disorders': _s('crit_locomotor_disorders', sc['crit_locomotor_disorders']),
        'norm_mastitis_disorders': _s('norm_mastitis_disorders', sc['norm_mastitis_disorders']),
        'warn_mastitis_disorders': _s('warn_mastitis_disorders', sc['warn_mastitis_disorders']),
        'crit_mastitis_disorders': _s('crit_mastitis_disorders', sc['crit_mastitis_disorders']),
        'matrix_warn_pct': _s('matrix_warn_pct', an['matrix_warn_pct']),
        'matrix_crit_pct': _s('matrix_crit_pct', an['matrix_crit_pct']),
        'high_yield_threshold_kg': int(_s('high_yield_threshold_kg', an['high_yield_threshold_kg']))
    }
    
    return render_template('dashboard.html',
                           user_type=user_type,
                           farm_name=farm_name,
                           selected_farm=selected_farm,
                           d=dashboard_data,
                           alerts=alerts,
                           settings=settings_data)

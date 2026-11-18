# config.py - Конфигурация приложения
import os
import secrets
from datetime import timedelta

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Автоматическая загрузка переменных из .env (если файл существует)
_env_file = os.path.join(BASE_DIR, '.env')
if os.path.exists(_env_file):
    with open(_env_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())


def _load_or_create_secret_key():
    """
    Загружает SECRET_KEY из окружения либо генерирует и сохраняет стойкий
    криптографический ключ в instance/.secret_key (вне репозитория).
    """
    env_key = os.environ.get('SECRET_KEY') or os.environ.get('SESSION_SECRET')
    if env_key and len(env_key.strip()) >= 16:
        return env_key.strip()

    instance_dir = os.path.join(BASE_DIR, 'instance')
    secret_path = os.path.join(instance_dir, '.secret_key')
    if os.path.exists(secret_path):
        try:
            with open(secret_path, 'r', encoding='utf-8') as sf:
                stored = sf.read().strip()
                if len(stored) >= 32:
                    return stored
        except OSError:
            pass

    new_key = secrets.token_hex(32)
    try:
        os.makedirs(instance_dir, mode=0o700, exist_ok=True)
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as sf:
            sf.write(new_key)
    except OSError:
        # На read-only ФС оставляем сгенерированный в памяти ключ
        pass
    return new_key


class Config:
    # Криптографический секретный ключ для подписи сессий и CSRF-токенов
    SECRET_KEY = _load_or_create_secret_key()

    # Настройки cookies и сессий (динамическое включение Secure по HTTPS в app.py)
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'False').lower() in ('true', '1', 'yes')
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Настройки CSRF-защиты (включена по умолчанию; корректно работает за ProxyFix)
    WTF_CSRF_ENABLED = os.environ.get('WTF_CSRF_ENABLED', 'True').lower() not in ('false', '0', 'no')
    WTF_CSRF_TIME_LIMIT = 31536000  # 1 год (привязано к сессии)
    WTF_CSRF_SSL_STRICT = False     # Корректная работа за reverse-proxy с X-Forwarded-Proto

    # База данных SQLite
    BASE_DIR = BASE_DIR
    DATABASE = os.environ.get('DATABASE_PATH') or os.path.join(BASE_DIR, 'cattle_disposal.db')

    # Максимальный размер загружаемого файла (16 MB)
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    # Организационные и справочные значения по умолчанию
    DEFAULT_ORG_NAME = 'ОАО «Новая Припять»'
    DEFAULT_UNP = '190000000'
    DEFAULT_ATTENDANT = 'Дежурный ветврач'
    DEFAULT_BREED = 'Черно-пёстрая белорусской селекции'
    DEFAULT_STILLBORN_MOTHER_GROUP = 'Полновозрастные коровы (2+ лакт.)'
    DEFAULT_STILLBORN_WEIGHT = 25.0
    DEFAULT_STILLBORN_GESTATION = 280

    # Канонические половозрастные группы скота (ПВГ) — единый источник для форм, фильтров и аналитики
    AGE_GROUPS = [
        'Телята (до 6 мес.)',
        'Ремонтный молодняк (6–18 мес.)',
        'Нетели (>18 мес.)',
        'Новотельные коровы (0–30 дн DIM)',
        'Коровы 1-й лактации (первотёлки)',
        'Полновозрастные коровы (2+ лакт.)',
        'Сухостойные коровы',
        'Бычки на откорме'
    ]

    # Породы скота
    BREEDS = [
        'Черно-пёстрая белорусской селекции',
        'Черно-пёстрая',
        'Голштинская',
        'Красная белорусская',
        'Симментальская',
        'Абердин-ангусская',
        'Герефордская',
        'Лимузин',
        'Помесь'
    ]

    # Зоотехнические, аналитические и матричные параметры по умолчанию
    DEFAULT_ANALYTICS_PARAMS = {
        'milk_yield_bin_1': 4500.0,
        'high_yield_threshold_kg': 6500.0,
        'milk_yield_bin_3': 8500.0,
        'matrix_warn_pct': 15.0,
        'matrix_crit_pct': 25.0,
        'farm_matrix_min_head': 10,
        'alert_window_short_days': 7,
        'alert_window_mid_days': 14,
        'alert_window_long_days': 30,
        'alert_death_min_count': 2,
        'alert_death_growth_ratio': 1.4,
        'alert_cluster_min_count': 3,
        'alert_calf_min_count': 3
    }

    # Светофорные пороги вет-скоркарда (Норма -> Внимание -> Критично)
    DEFAULT_SCORECARD_THRESHOLDS = {
        'norm_fresh_cow_disposal': 10.0,
        'warn_fresh_cow_disposal': 14.0,
        'crit_fresh_cow_disposal': 18.0,
        'norm_first_lactation_culling': 20.0,
        'warn_first_lactation_culling': 25.0,
        'crit_first_lactation_culling': 30.0,
        'norm_mortality_rate': 25.0,
        'warn_mortality_rate': 35.0,
        'crit_mortality_rate': 45.0,
        'norm_locomotor_disorders': 15.0,
        'warn_locomotor_disorders': 20.0,
        'crit_locomotor_disorders': 25.0,
        'norm_mastitis_disorders': 15.0,
        'warn_mastitis_disorders': 20.0,
        'crit_mastitis_disorders': 25.0
    }

    # Автоподстановка срока стельности и веса плода по типу акушерского события
    STILLBORN_PREFILL_MAP = [
        {'match': '1-й триместр', 'gestation': 65, 'weight': 0.5},
        {'match': '2-й триместр', 'gestation': 140, 'weight': 4.5},
        {'match': '3-й триместр', 'gestation': 220, 'weight': 14.0},
        {'match': 'в срок', 'gestation': 280, 'weight': 32.0},
        {'match': 'патология родов', 'gestation': 280, 'weight': 34.0},
        {'match': 'мумификация', 'gestation': 190, 'weight': 4.0},
        {'match': 'мацерация', 'gestation': 170, 'weight': 3.5},
        {'match': 'уродство', 'gestation': 260, 'weight': 22.0}
    ]

    # Базовые категории выбытия скота и их причины по умолчанию
    DISPOSAL_CATEGORIES = {
        'падёж': [
            'Патологические роды',
            'Моцерация плода',
            'Травма позвоночного столба',
            'Обширное повреждение мягких тканей',
            'Цирроз печени',
            'Острая тимпания рубца',
            'Гастроэнтерит',
            'Бронхопневмония',
            'Проводная язва сычуга',
            'Перитонит',
            'Травматический ретикулит / ретикулоперитонит',
            'Разрыв селезенки',
            'Пневмоторакс',
            'Сальпингит',
            'Диспепсия телят',
            'Гипотрофия',
            'Разрыв маточной артерии',
            'Разрыв печени',
            'Внутреннее кровотечение',
            'Интоксикация / кормовое отравление',
            'Другое'
        ],
        'выбраковка': [
            'Мастит хронический (атрофия долей)',
            'Агалактия (потеря молочной продуктивности)',
            'Гинекологические болезни (хронический эндометрит)',
            'Яловость (более 120 дней)',
            'Болезни копытец и суставов (хромота)',
            'Фолликулярная / лютеиновая киста яичников',
            'Возрастная выбраковка (старше 5 лактаций)',
            'Фримартинизм',
            'Травма соска / вымени',
            'Другое'
        ],
        'санитарный': [
            'Травма конечностей (перелом)',
            'Травма позвоночного столба',
            'Стойкая атония преджелудков',
            'Патологические роды (угроза гибели)',
            'Кетоз тяжелой степени',
            'Гнойно-катаральный мастит',
            'Выпадение матки (необратимое)',
            'Отек легких',
            'Другое'
        ]
    }

    # Виды событий мертворождений и абортов (Акушерский журнал)
    STILLBIRTH_EVENT_TYPES = [
        'Мертворождённый телёнок (в срок)',
        'Аборт (1-й триместр, до 90 дн.)',
        'Аборт (2-й триместр, 90–180 дн.)',
        'Аборт (3-й триместр, 181+ дн.)',
        'Патология родов / Асфиксия плода',
        'Мумификация плода',
        'Мацерация плода',
        'Уродство / аномалия развития плода'
    ]

    # Специализированные причины мертворождений и внутриутробной гибели
    STILLBIRTH_CAUSES = [
        'Асфиксия плода (тяжёлые роды / крупноплодие)',
        'Внутриутробная гипоксия',
        'Скручивание (заворот) матки',
        'Узкий таз матери / несоответствие плода родовым путям',
        'Неправильное членорасположение плода',
        'Слабость родовой деятельности / атония матки',
        'Травма таза / ушиб живота матери',
        'Инфекционный аборт (хламидиоз / ИРТ / бруцеллёз)',
        'Плацентит / патология плаценты',
        'Кормовой микотоксикоз / интоксикация матери',
        'Генетическое уродство / водянка плода',
        'Двойня (слабость второго плода)',
        'Антенатальная гибель плода неясной этиологии',
        'Другое'
    ]

    # Пол плода
    FETUS_SEXES = [
        'Тёлочка',
        'Бычок',
        'Двойня (бычок + тёлочка)',
        'Двойня (тёлочки)',
        'Двойня (бычки)',
        'Не сформирован / не определён'
    ]

    # Физиологическое состояние коровы-матери после отёла/аборта
    MOTHER_CONDITIONS = [
        'Здорова, оставлена в стаде',
        'На лечении: задержание последа',
        'На лечении: острый эндометрит',
        'На лечении: травма родовых путей',
        'Вынужденный убой матери (патология родов)',
        'Падёж матери в родах'
    ]

    # Группы матерей
    MOTHER_GROUPS = [
        'Коровы 1-й лактации (первотёлки)',
        'Полновозрастные коровы (2+ лакт.)',
        'Нетели (>18 мес., случной возраст)',
        'Сухостойные коровы'
    ]

    @classmethod
    def infer_stillborn_defaults(cls, event_type):
        """Подбор дефолтных дней стельности и массы плода по названию события."""
        et_low = (event_type or '').lower()
        for item in cls.STILLBORN_PREFILL_MAP:
            if item['match'] in et_low:
                return item['gestation'], item['weight']
        return cls.DEFAULT_STILLBORN_GESTATION, cls.DEFAULT_STILLBORN_WEIGHT

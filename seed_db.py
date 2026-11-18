# seed_db.py - Наполнение демонстрационными данными сельхозорганизации РБ
import sqlite3
from datetime import datetime, timedelta
from config import Config
from auth import register_user

from domain import classify_pathology_system, compute_lactation_phase


def seed_sample_cows():
    import secrets
    from database import get_db_connection, init_db
    init_db()

    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Создаём демонстрационные фермы только при их отсутствии (без захардкоженных паролей)
    cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE user_type = 'farm'")
    if cursor.fetchone()['cnt'] == 0:
        for uname, fname in (('farm1', 'МТФ-1 Центральная'), ('farm2', 'МТФ-2 Заречье'), ('farm3', 'МТФ-3 Полесье')):
            register_user(uname, secrets.token_urlsafe(12), 'farm', fname, must_change_password=True)

    # Проверяем, есть ли уже записи в таблице cows
    cursor.execute("SELECT COUNT(*) as cnt FROM cows WHERE COALESCE(is_stillborn, 0) = 0")
    count = cursor.fetchone()['cnt']
    if count >= 1000:
        conn.close()
        return

    import random
    print("Генерация масштабного массива данных (1 250+ коров) для хозяйств РБ...")
    
    # Получаем id пользователей ферм
    cursor.execute("SELECT id, farm_name FROM users WHERE user_type = 'farm'")
    farm_users = {r['farm_name']: r['id'] for r in cursor.fetchall()}
    
    now = datetime.now()
    
    farms = [
        ("МТФ-1 Центральная", "МТФ1"),
        ("МТФ-2 Заречье", "МТФ2"),
        ("МТФ-3 Полесье", "МТФ3")
    ]
    
    reasons_pad = [
        "Острая тимпания рубца", "Бронхопневмония", "Диспепсия телят",
        "Травматический ретикулит", "Кетоз (острая форма)", "Анаэробная энтеротоксемия",
        "Эшерихиоз (колибактериоз)", "Перитонит", "Послеродовой парез", "Разрыв маточной артерии"
    ]
    reasons_vyb = [
        "Гнойный мастит", "Агалактия (потеря молочности)", "Хронический гнойный эндометрит",
        "Фолликулярная киста яичников", "Лютеиновая киста", "Язва Рунхольца (пододерматит)",
        "Флегмона венчика", "Атрофия долей вымени", "Атрофия яичников / яловость",
        "Возрастная выбраковка (предельный возраст)"
    ]
    reasons_san = [
        "Травма конечностей", "Травма позвоночного столба", "Патологические роды",
        "Выпадение матки", "Стойкая атония преджелудков", "Кетоз тяжелой степени"
    ]
    
    counters = {}
    total_to_generate = 1250
    
    for i in range(total_to_generate):
        r_f = random.random()
        farm, f_prefix = farms[0] if r_f < 0.42 else (farms[1] if r_f < 0.76 else farms[2])
        counters[farm] = counters.get(farm, 0) + 1
        cow_num = f"{f_prefix}-{counters[farm]:04d}"
        
        r_cat = random.random()
        if r_cat < 0.28:
            cat = "падёж"
            reason = random.choice(reasons_pad)
        elif r_cat < 0.84:
            cat = "выбраковка"
            reason = random.choice(reasons_vyb)
        else:
            cat = "санитарный"
            reason = random.choice(reasons_san)
            
        days_ago = int((random.random() ** 1.05) * 360)
        disposal_d = (now - timedelta(days=days_ago)).strftime('%Y-%m-%d')
        tag = f"BY 04 {7100000 + i}"
        breed = random.choice([Config.DEFAULT_BREED, "Голштинская", "Лимузин"])
        
        lact = random.randint(1, 6)
        ag = "Коровы 1-й лактации (первотёлки)" if lact == 1 else "Полновозрастные коровы (2+ лакт.)"
        w = float(random.randint(480, 660))
        milk = float(random.randint(5500, 9200))
        
        if "телят" in reason.lower() or "эшерихиоз" in reason.lower():
            ag = "Телята (до 6 мес.)"
            lact = 0
            w = float(random.randint(32, 85))
            milk = 0.0
        elif "роды" in reason.lower() or "матки" in reason.lower():
            ag = "Нетели (>18 мес.)"
            lact = 0
            w = float(random.randint(430, 530))
            milk = 0.0
            
        user_id = farm_users.get(farm, 1)
            
        lact_phase = compute_lactation_phase(ag, None, lact)
        p_sys = classify_pathology_system(reason)
        cursor.execute('''
            INSERT INTO cows (cow_id, ear_tag, farm_name, category, reason, disposal_date, 
                              lactation, lactation_phase, pathology_system, weight, notes, age_group, breed, milk_yield, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (cow_num, tag, farm, cat, reason, disposal_d, 
              lact, lact_phase, p_sys, w, f"Выбытие скота ({cat})", ag, breed, milk, user_id))
              
    conn.commit()
    conn.close()
    print("1 250 записей успешно загружены в базу SQLite!")

if __name__ == '__main__':
    seed_sample_cows()

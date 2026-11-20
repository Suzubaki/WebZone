# Инструкция по развертыванию на PythonAnywhere

## Шаги для запуска:
1. Загрузите файлы проекта в директорию `/home/<ваш-логин>/WEB`.
2. Откройте консоль Bash на PythonAnywhere и выполните:
   ```bash
   cd ~/WEB
   pip install --user -r requirements.txt
   # Опционально задайте свой ключ и начальный пароль администратора:
   # export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
   # export INITIAL_ADMIN_PASSWORD="СложныйВременныйПароль!"
   python3 create_users.py
   python3 check_system.py
   ```
   Если переменная `INITIAL_ADMIN_PASSWORD` не задана, скрипт при пустой БД сгенерирует случайный пароль для `admin`, выведет его в консоль и сохранит в `instance/initial_credentials.txt` (удалите файл после сохранения пароля).
3. В разделе **Web** на PythonAnywhere:
   - В поле **Source code** укажите: `/home/<ваш-логин>/WEB`
   - В поле **Working directory** укажите: `/home/<ваш-логин>/WEB`
   - Откройте файл конфигурации **WSGI** (ссылка на странице Web) и вставьте:
     ```python
     import sys
     import os

     path = '/home/<ваш-логин>/WEB'
     if path not in sys.path:
         sys.path.insert(0, path)

     os.chdir(path)

     # Рекомендуется задать постоянный SECRET_KEY в окружении или использовать instance/.secret_key
     # os.environ.setdefault('SECRET_KEY', '<ваш-сгенерированный-ключ>')

     from app import app as application
     ```
4. Нажмите зелёную кнопку **Reload <ваш-логин>.pythonanywhere.com**.
5. Войдите под пользователем `admin`. Все учётные записи ферм и их пароли создаются, изменяются и выдаются исключительно администратором через меню **Администрирование → Пользователи ферм / Создать аккаунт**.

## Очистка базы данных перед запуском в работу

### Вариант 1: Удалить только тестовые записи (сохранив пользователей, фермы, причины и нормативы)
Выполните в Bash-консоли PythonAnywhere в папке проекта:
```bash
python3 -c "from database import run_write_transaction; run_write_transaction(lambda c, cur: [cur.execute(q) for q in ('DELETE FROM cows', 'DELETE FROM stillbirth_records', 'DELETE FROM farm_counters', 'DELETE FROM audit_logs', \"DELETE FROM sqlite_sequence WHERE name IN ('cows','stillbirth_records','audit_logs')\")])"
```
После этого нажмите **Reload** на вкладке **Web**.

### Вариант 2: Полный сброс базы данных с нуля
```bash
rm -f cattle_disposal.db cattle_disposal.db-wal cattle_disposal.db-shm
python3 create_users.py
```
После этого нажмите **Reload** на вкладке **Web**.

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
   Если переменная `INITIAL_ADMIN_PASSWORD` не задана, скрипт при пустой БД сгенерирует случайный временный пароль для `admin`, выведет его в консоль и сохранит в `instance/initial_credentials.txt` (удалите файл после первого входа). При первом входе система потребует сменить пароль.
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
5. Войдите под пользователем `admin` с временным паролем и сразу задайте постоянный пароль на странице смены пароля. Создайте учётные записи ферм через меню **Администрирование → Создать аккаунт**.

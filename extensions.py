# extensions.py - Централизованная инициализация расширений Flask
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()

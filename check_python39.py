# check_python39.py
import sys
from importlib import metadata as importlib_metadata


def _pkg_version(name, fallback_mod=None):
    try:
        return importlib_metadata.version(name)
    except Exception:
        return getattr(fallback_mod, '__version__', 'installed')


print(f"Python Version: {sys.version}")
try:
    import flask
    import flask_wtf
    import werkzeug
    import openpyxl
    print("Flask:", _pkg_version('Flask', flask))
    print("Flask-WTF:", _pkg_version('Flask-WTF', flask_wtf))
    print("Werkzeug:", _pkg_version('Werkzeug', werkzeug))
    print("Openpyxl:", _pkg_version('openpyxl', openpyxl))
    print("All packages successfully imported!")
except ImportError as e:
    print("Import error:", e)
    sys.exit(1)

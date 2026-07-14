import os
import sys

# Make the app package dir (app/) importable so `import app` / `import history`
# resolve to app/app.py and app/history.py.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

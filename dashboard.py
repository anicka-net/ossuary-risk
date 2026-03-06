"""Redirect: use 'ossuary dashboard' instead. Kept for backward compatibility."""
import subprocess
import sys
import os

app_path = os.path.join(os.path.dirname(__file__), "src", "ossuary", "dashboard", "app.py")
sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", app_path] + sys.argv[1:]))

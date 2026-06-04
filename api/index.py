import sys
import os

# Add parent directory to path so server.py can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app

# Vercel expects the WSGI app to be named 'app' or 'handler'
handler = app

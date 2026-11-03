import sys
import os

# Project root যোগ করো যাতে app.py, protobuf ফাইল পাওয়া যায়
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app

# Vercel Python handler
handler = app

"""Vercel Python HTTP entry point; no daemon or local persistent files."""
import os
import sys
from pathlib import Path

os.environ['SERVERLESS'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from server import Handler

handler = Handler

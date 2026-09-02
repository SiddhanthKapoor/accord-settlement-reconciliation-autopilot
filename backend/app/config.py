import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")

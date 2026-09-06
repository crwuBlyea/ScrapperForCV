# config.py
import os
from pathlib import Path

BASE_DIR = Path(r"Enter Path")
IMAGES_DIR = BASE_DIR / "images"
METADATA_FILE = BASE_DIR / "metadata.csv"

IMAGES_DIR.mkdir(parents=True, exist_ok=True)
(IMAGES_DIR / "human").mkdir(exist_ok=True)
(IMAGES_DIR / "ai").mkdir(exist_ok=True)

SUBREDDITS_HUMAN = []
SUBREDDITS_AI = ["Midjourney"]

MAX_POSTS_PER_SUB = 2000
CIVITAI_LIMIT = 0

# Twitter Settings
TWITTER_TAGS = ["anime_art", "concept_art", "ai_art"]
TWITTER_LIMIT = 600

# --- TWITTER COOKIES ---
# Установите переменные окружения:
# export TWITTER_AUTH_TOKEN=...
# export TWITTER_CT0=...
_TWITTER_AUTH = os.environ.get("TWITTER_AUTH_TOKEN", "")
_TWITTER_CT0 = os.environ.get("TWITTER_CT0", "")

if _TWITTER_AUTH and _TWITTER_CT0:
    TWITTER_COOKIES = [
        {"name": "auth_token", "value": _TWITTER_AUTH, "domain": ".x.com", "path": "/"},
        {"name": "ct0", "value": _TWITTER_CT0, "domain": ".x.com", "path": "/"}
    ]
else:
    TWITTER_COOKIES = []

# ArtStation Settings
ARTSTATION_QUERIES_HUMAN = ["digital art", "concept art", "character design"]
ARTSTATION_QUERIES_AI = ["midjourney", "stable diffusion"]
ARTSTATION_LIMIT = 0

# --- PIXIV RANKING SETTINGS ---
# Вместо поиска по тегам — используем официальный ранкинг
PIXIV_USE_RANKING = True          # True = ранкинг, False = поиск (как раньше)
PIXIV_RANKING_MODE = "month"       # day | week | month | day_male | day_female | week_original | week_rookie
PIXIV_RANKING_DATE = "2025-12-14"           # "" = вчерашний день, или "2026-08-14" для конкретной даты
PIXIV_LIMIT = 500
PIXIV_LABEL = "human"             # human или ai

# Оставьте старые настройки на всякий случай
PIXIV_REFRESH_TOKEN = "eRBbAx01xzD1lHCgmU2OuH2oodADekZUaFdSHMX6kSM"
PIXIV_PROXY = ""
PIXIV_QUERIES_HUMAN = [
    "digital art",
    "concept art", 
    "character design",
    "fantasy art",
    "landscape"
]

PIXIV_MIN_BOOKMARKS = 2500   # Минимум закладок (ブックマーク)
PIXIV_MIN_VIEWS = 12000    # Минимум просмотров (閲覧数)
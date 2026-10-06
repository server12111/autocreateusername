from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str
    ADMIN_IDS: str = ""
    # Папка для всех файлов бота (БД, сессия бота). На хостинге — та, что сохраняется при перезапуске,
    # например на Bothost это /app/data (переменная DATA_DIR)
    DATA_DIR: str = "data"
    DATABASE_URL: str = ""  # пусто — SQLite в DATA_DIR/bot.db

    API_ID: int = 0
    API_HASH: str = ""
    SESSIONS_DIR: str = "sessions"

    TGRASS_API_KEY: str = ""
    SNIPER_INTERVAL: int = 120
    HTTP_PROXY: str = ""

    # Баннеры разделов (assets/banners/*.jpg); пусто — без баннеров
    BANNERS_URL: str = "https://raw.githubusercontent.com/server12111/autocreateusername/main/assets/banners"

    # Оплата в долларах: Crypto Pay API (@CryptoBot) и xRocket Pay API
    CRYPTOBOT_TOKEN: str = ""
    XROCKET_TOKEN: str = ""

    @model_validator(mode="after")
    def _defaults_in_data_dir(self) -> "Settings":
        if not self.DATABASE_URL:
            self.DATABASE_URL = f"sqlite+aiosqlite:///{self.DATA_DIR}/bot.db"
        return self

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.ADMIN_IDS.replace(" ", "").split(",") if x}


settings = Settings()

# Тарифы Premium: ключ -> (дни, цена в Stars)
PREMIUM_PLANS = {
    "p1": (1, 40),
    "p3": (3, 99),
    "p10": (10, 289),
    "p30": (30, 650),
    "p90": (90, 1499),
}
# Пакеты поисков: ключ -> (кол-во поисков, цена в Stars)
SEARCH_PACKS = {
    "s10": (10, 49),
    "s50": (50, 249),
    "s150": (150, 649),
}
# Цены в долларах для оплаты через CryptoBot / xRocket: ключ товара -> USD
PRICES_USD = {
    "p1": "0.50",
    "p3": "1.20",
    "p10": "3.49",
    "p30": "7.99",
    "p90": "18.49",
    "s10": "0.60",
    "s50": "3.99",
    "s150": "9.99",
}
# Уровни реферальных наград: (кол-во друзей, дни Premium)
REF_TIERS = [(7, 1), (9, 3), (18, 10), (35, 25)]


def plan_period(days: int) -> str:
    """Срок тарифа по-человечески: 1 день, 3 дня, 10 дней, 1 месяц, 3 месяца."""
    if days % 30 == 0:
        months = days // 30
        return f"{months} {plural(months, 'месяц', 'месяца', 'месяцев')}"
    return f"{days} {plural(days, 'день', 'дня', 'дней')}"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


# Значения по умолчанию для таблицы settings
DEFAULT_SETTINGS = {
    "tgrass_api_key": "",
    "tgrass_enabled": "0",
    "botohub_api_key": "",
    "botohub_enabled": "0",
    "start_free_searches": "1",
    "sponsor_bonus": "2",
    "search_cooldown_sec": "20",
    "support_url": "https://t.me/avalnm",
    "battle_enabled": "1",
    "captcha_enabled": "0",
    "renew_discount_pct": "20",
    "premium_daily_limit": "25",  # найденных юзернеймов в сутки (МСК) для Premium; 0 — без лимита
    "premium_emoji": "{}",
    "premium_emoji_enabled": "1",
    "button_colors_enabled": "1",
}

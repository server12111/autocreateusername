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
    "p1": (1, 10),
    "p3": (3, 25),
    "p10": (10, 65),
    "p30": (30, 100),
}
# Пакеты поисков: ключ -> (кол-во поисков, цена в Stars)
SEARCH_PACKS = {
    "s10": (10, 5),
    "s50": (50, 20),
    "s150": (150, 50),
}
# Цены в долларах для оплаты через CryptoBot / xRocket: ключ товара -> USD
PRICES_USD = {
    "p1": "0.12",
    "p3": "0.30",
    "p10": "0.77",
    "p30": "1.19",
    "s10": "0.06",
    "s50": "0.24",
    "s150": "0.60",
}
# Уровни реферальных наград: (кол-во друзей, дни Premium)
REF_TIERS = [(3, 1), (10, 3), (15, 10), (35, 25)]

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
    "premium_emoji": "{}",
    "premium_emoji_enabled": "1",
    "button_colors_enabled": "1",
}

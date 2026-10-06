"""Баннеры разделов: картинка показывается над текстом сообщения как большое превью ссылки.

Так все экраны остаются обычными текстовыми сообщениями (до 4096 символов, правка через edit_text),
а сверху — баннер. Картинки лежат в assets/banners/ и раздаются с GitHub (репозиторий публичный);
другой адрес можно задать в .env: BANNERS_URL.
"""

from aiogram.types import LinkPreviewOptions

from config import settings

# Telegram кэширует превью по ссылке: при замене картинок увеличьте версию
VERSION = 2
NAMES = {"main", "search", "social", "premium", "packs", "profile", "ref", "battle", "trap"}


def preview(name: str | None) -> LinkPreviewOptions:
    """Превью с баннером над текстом; без баннера — превью ссылок выключено, как раньше."""
    if not name or name not in NAMES or not settings.BANNERS_URL:
        return LinkPreviewOptions(is_disabled=True)
    return LinkPreviewOptions(
        url=f"{settings.BANNERS_URL.rstrip('/')}/{name}.jpg?v={VERSION}",
        prefer_large_media=True,
        show_above_text=True,
    )

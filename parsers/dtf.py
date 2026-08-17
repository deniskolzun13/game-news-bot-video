from urllib.parse import urlsplit

from parsers.rss_base import RssParser

# Разделы DTF, относящиеся к играм (остальные — кино, аниме и т.п. — отсекаем).
GAME_SECTIONS = ("/games/", "/gameindustry/", "/gamedev/")


class DtfParser(RssParser):
    """DTF: общая RSS-лента https://dtf.ru/rss/all, фильтр по игровым разделам."""

    name = "dtf"
    feed_url = "https://dtf.ru/rss/all"

    def _is_relevant(self, item) -> bool:
        path = urlsplit(item.get("link", "")).path
        return path.startswith(GAME_SECTIONS)
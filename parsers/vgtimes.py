from urllib.parse import urlsplit

from parsers.rss_base import RssParser

# Только игровые разделы VGTimes (кино, железо, косплей — отсекаем).
GAME_SECTIONS = ("/gaming-news/", "/articles/")


class VgTimesParser(RssParser):
    """VGTimes: общая RSS-лента https://vgtimes.ru/rss.xml, фильтр по игровым разделам."""

    name = "vgtimes"
    feed_url = "https://vgtimes.ru/rss.xml"

    def _is_relevant(self, item) -> bool:
        path = urlsplit(item.get("link", "")).path
        return path.startswith(GAME_SECTIONS)
from parsers.rss_base import RssParser


class IgromaniaParser(RssParser):
    """Igromania: RSS-лента https://www.igromania.ru/rss/news.xml."""

    name = "igromania"
    feed_url = "https://www.igromania.ru/rss/news.xml"
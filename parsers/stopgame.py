from parsers.rss_base import RssParser


class StopGameParser(RssParser):
    """StopGame: RSS-лента https://rss.stopgame.ru/rss_news.xml."""

    name = "stopgame"
    feed_url = "https://rss.stopgame.ru/rss_news.xml"
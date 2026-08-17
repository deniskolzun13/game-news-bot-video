from parsers.rss_base import RssParser


class ThreeDNewsParser(RssParser):
    """3DNews: RSS игрового раздела https://3dnews.ru/games/rss/."""

    name = "3dnews"
    feed_url = "https://3dnews.ru/games/rss/"
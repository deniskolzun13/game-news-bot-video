from parsers.dtf import DtfParser
from parsers.igromania import IgromaniaParser
from parsers.stopgame import StopGameParser
from parsers.threednews import ThreeDNewsParser
from parsers.vgtimes import VgTimesParser

PARSERS = {
    "stopgame": StopGameParser,
    "igromania": IgromaniaParser,
    "dtf": DtfParser,
    "3dnews": ThreeDNewsParser,
    "vgtimes": VgTimesParser,
}


def build_parsers(client, enabled_sources: list[str]) -> list:
    """Возвращает список экземпляров парсеров для включённых источников."""
    parsers = []
    for name in enabled_sources:
        cls = PARSERS.get(name.lower())
        if cls is None:
            raise SystemExit(f"Неизвестный источник в ENABLED_SOURCES: {name!r}. "
                             f"Допустимые: {', '.join(PARSERS)}")
        parsers.append(cls(client))
    return parsers
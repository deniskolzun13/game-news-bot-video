import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging(log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    ):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)
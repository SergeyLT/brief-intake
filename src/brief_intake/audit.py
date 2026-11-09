"""Локальная команда краткого просмотра аудита без текстов заявок."""

import argparse
import json
import os
import sqlite3
from pathlib import Path

from brief_intake.storage import Store


def main() -> None:
    """Показать последние записи выбранной SQLite; ключ модели не нужен."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("DATABASE_PATH", "output/briefs.sqlite3")),
        help="Путь к SQLite",
    )
    parser.add_argument("--limit", type=int, default=10, help="Количество последних записей")
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("Количество записей должно быть от 1 до 1000")
    if not args.database.is_file():
        parser.error("База не найдена; сначала запустите приложение")
    try:
        print(json.dumps(Store(args.database).audit(args.limit), ensure_ascii=False, indent=2))
    except sqlite3.Error:
        parser.exit(1, "Не удалось прочитать базу аудита\n")


if __name__ == "__main__":
    main()

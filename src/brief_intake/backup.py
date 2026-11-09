"""Согласованная резервная копия работающей SQLite через Backup API."""

import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def create_backup(source: Path, target: Path) -> None:
    """Создать новый файл копии, не перезаписывая существующий."""
    if not source.is_file():
        raise ValueError("Исходная база не найдена")
    if source.resolve() == target.resolve():
        raise ValueError("Копия не может совпадать с исходной базой")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Режим xb резервирует именно новый путь: существующая копия не перезаписывается.
    with target.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(target)) as destination:
                original.backup(destination)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def main() -> None:
    """Сохранить копию в заданный путь внутри доступного каталога."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="Новый файл резервной копии")
    args = parser.parse_args()
    source = Path(os.getenv("DATABASE_PATH", "output/briefs.sqlite3"))
    try:
        create_backup(source, args.target)
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(1, "Копия не создана: проверьте исходную базу и новый путь назначения\n")
    print("Резервная копия создана; перенесите её в отдельное защищённое хранилище")


if __name__ == "__main__":
    main()

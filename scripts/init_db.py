#!/usr/bin/env python3
"""Initialize the PARA Organizer database (creates tables, FTS5, indexes)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import init_db  # noqa: E402


async def main() -> None:
    await init_db()
    print(f"Database initialized at {settings.PARA_DB_PATH}")


if __name__ == "__main__":
    asyncio.run(main())

"""Self-contained migration: SQLite -> PostgreSQL. No app imports."""
import asyncio, aiosqlite, asyncpg, json, os

SQLITE_PATH = os.environ.get("PARA_DB_PATH", "/var/lib/para-organizer/data/para.db")
PG_DSN = os.environ.get("PARA_DB_URL", "postgresql+asyncpg://para:password@para-db:5432/para").replace("+asyncpg", "")

TABLES = [
    ("notes", ["id","title","content","para_category","sub_category","status","priority","deadline","tags","source","source_metadata","llm_model","llm_confidence","llm_reasoning","embedding_status","recurrence","created_at","updated_at","archived_at"]),
    ("links", ["id","from_note_id","to_note_id","link_type","created_at"]),
    ("history", ["id","note_id","action","old_value","new_value","reason","timestamp"]),
    ("notifications", ["id","note_id","type","channel","status","scheduled_at","sent_at","payload"]),
    ("settings", ["key","value"]),
    ("chat_messages", ["id","chat_id","role","content","created_at"]),
    ("llm_usage", ["id","ts","model","task","prompt_tokens","completion_tokens","note_id"]),
    ("events", ["id","event_type","note_id","payload","status","created_at","delivered_at"]),
    ("tasks", ["id","prompt","status","note_id","created_at","updated_at"]),
    ("items", ["id","note_id","text","done","created_at"]),
    ("feedback", ["id","field","llm_value","user_value","note_content_snippet","created_at"]),
]

def fix_types(d):
    from datetime import date, datetime
    d = dict(d)
    for k, v in list(d.items()):
        if isinstance(v, str):
            for fn in (datetime.fromisoformat, date.fromisoformat):
                try:
                    d[k] = fn(v)
                    break
                except (ValueError, TypeError):
                    continue
        if isinstance(v, (dict, list)):
            d[k] = json.dumps(v, ensure_ascii=False)
    d.pop("embedding", None)
    return d

async def main():
    sqlite = await aiosqlite.connect(SQLITE_PATH)
    sqlite.row_factory = aiosqlite.Row
    await sqlite.execute("PRAGMA foreign_keys=OFF")
    pg = await asyncpg.connect(PG_DSN)

    # Truncate all tables first
    for t, _ in TABLES:
        await pg.execute(f'TRUNCATE "{t}" CASCADE')

    for table, cols in TABLES:
        # Check if table exists in SQLite
        tc = await sqlite.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not await tc.fetchone():
            print(f"  {table}: not in SQLite, skip")
            continue
        col_list = ", ".join(f'"{c}"' for c in cols)
        ph = ", ".join(f"${i+1}" for i in range(len(cols)))
        c = await sqlite.execute(f"SELECT COUNT(*) FROM {table}")
        count = (await c.fetchone())[0]
        if count == 0:
            print(f"  {table}: empty, skip")
            continue
        has_id = "id" in cols
        offset = 0
        total = 0
        while True:
            if has_id:
                c = await sqlite.execute(f"SELECT {col_list} FROM {table} ORDER BY id LIMIT ? OFFSET ?", (500, offset))
            else:
                c = await sqlite.execute(f"SELECT {col_list} FROM {table} LIMIT ? OFFSET ?", (500, offset))
            rows = await c.fetchall()
            if not rows:
                break
            batch = [tuple(fix_types(r).get(c) for c in cols) for r in rows]
            await pg.executemany(f'INSERT INTO "{table}" ({col_list}) VALUES ({ph})', batch)
            total += len(batch)
            offset += 500
        pg_count = await pg.fetchval(f'SELECT COUNT(*) FROM "{table}"')
        ok = "OK" if count == pg_count else "MISMATCH"
        print(f"  {table}: {count} -> {pg_count} [{ok}]")

    await sqlite.close()
    await pg.close()
    print("Migration complete!")

asyncio.run(main())

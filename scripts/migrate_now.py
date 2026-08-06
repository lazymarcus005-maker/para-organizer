import asyncio, aiosqlite, asyncpg, json

SQLITE_PATH = "/var/lib/para-organizer/data/para.db"
PG_DSN = "postgresql://para:password@para-db:5432/para"

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

def transform(d):
    d = dict(d)
    for col in ["tags","source_metadata","recurrence","payload"]:
        if col in d and isinstance(d.get(col), str):
            try: d[col] = json.loads(d[col])
            except: d[col] = {} if col != "recurrence" else None
    d.pop("embedding", None)
    return d

async def main():
    sqlite = await aiosqlite.connect(SQLITE_PATH)
    sqlite.row_factory = aiosqlite.Row
    await sqlite.execute("PRAGMA foreign_keys=OFF")
    pg = await asyncpg.connect(PG_DSN)
    for table, cols in TABLES:
        col_list = ", ".join(cols)
        ph = ", ".join(f"${i+1}" for i in range(len(cols)))
        c = await sqlite.execute(f"SELECT COUNT(*) FROM {table}")
        count = (await c.fetchone())[0]
        if count == 0:
            print(f"  {table}: empty, skip")
            continue
        c = await sqlite.execute(f"SELECT {col_list} FROM {table} ORDER BY id")
        rows = await c.fetchall()
        batch = [tuple(transform(r).get(c) for c in cols) for r in rows]
        await pg.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({ph})", batch)
        pg_count = await pg.fetchval(f"SELECT COUNT(*) FROM {table}")
        ok = "OK" if count == pg_count else "MISMATCH"
        print(f"  {table}: {count} -> {pg_count} [{ok}]")
    await sqlite.close()
    await pg.close()
    print("Migration complete!")

asyncio.run(main())

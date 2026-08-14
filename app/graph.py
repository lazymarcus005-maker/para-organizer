"""Knowledge graph reasoning over the links table (SB-05).

Works against either backend (auto-detected by the connection type):
- ``aiosqlite.Connection`` (SQLite, local-dev / legacy)
- SQLAlchemy ``AsyncSession`` (PostgreSQL, production)

Each public function opens its own connection / session if not given one, so it
can be called both as a top-level coroutine from a scheduled job and as a
FastAPI dependency-injected call.
"""

from __future__ import annotations

import logging
from collections import deque

from app.config import settings
from app.database_v2 import async_session_factory
from app.models_v2 import Link as PgLink
from app.models_v2 import Note as PgNote
from sqlalchemy import or_, select

logger = logging.getLogger("para.graph")


def _is_async_session(db) -> bool:
    return hasattr(db, "add") and hasattr(db, "flush") and not hasattr(db, "execute_fetchall")


# ── Internal helpers (SQLite row-style) ─────────────────────────────────────


def _row_get(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    # SQLAlchemy ORM instance or aiosqlite Row
    return getattr(row, key, default)


# ── Full graph ───────────────────────────────────────────────────────────────


async def get_full_graph() -> dict:
    if settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL:
        async with async_session_factory() as session:
            note_rows = (await session.execute(
                select(PgNote.id, PgNote.title, PgNote.para_category, PgNote.status)
            )).all()
            link_rows = (await session.execute(
                select(PgLink.from_note_id, PgLink.to_note_id, PgLink.link_type)
            )).all()
        nodes = [
            {"id": r.id, "title": r.title, "para_category": r.para_category, "status": r.status}
            for r in note_rows
        ]
        edges = [
            {"from_id": r.from_note_id, "to_id": r.to_note_id, "link_type": r.link_type}
            for r in link_rows
        ]
    else:
        from app.database import get_connection
        async with get_connection() as db:
            cursor = await db.execute("SELECT id, title, para_category, status FROM notes")
            note_rows = await cursor.fetchall()
            cursor = await db.execute("SELECT from_note_id, to_note_id, link_type FROM links")
            link_rows = await cursor.fetchall()
        nodes = [
            {"id": r["id"], "title": r["title"], "para_category": r["para_category"], "status": r["status"]}
            for r in note_rows
        ]
        edges = [
            {"from_id": r["from_note_id"], "to_id": r["to_note_id"], "link_type": r["link_type"]}
            for r in link_rows
        ]

    logger.info("get_full_graph nodes=%d edges=%d", len(nodes), len(edges))
    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# ── Subgraph (BFS up to depth) ──────────────────────────────────────────────


async def get_subgraph(note_id: int, depth: int = 2) -> dict:
    if settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL:
        return await _get_subgraph_pg(note_id, depth)
    return await _get_subgraph_sqlite(note_id, depth)


async def _get_subgraph_pg(note_id: int, depth: int) -> dict:
    async with async_session_factory() as session:
        root_row = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.para_category, PgNote.status)
            .where(PgNote.id == note_id)
        )).first()
        if root_row is None:
            return {
                "root": None, "nodes": [], "edges": [],
                "depth": depth, "node_count": 0, "edge_count": 0,
            }
        root = {"id": root_row.id, "title": root_row.title, "para_category": root_row.para_category}

        visited: dict[int, int] = {note_id: 0}
        edges: list[dict] = []
        seen_edges: set[tuple[int, int]] = set()
        frontier: deque[int] = deque([note_id])

        while frontier:
            current = frontier.popleft()
            current_depth = visited[current]
            if current_depth >= depth:
                continue
            link_rows = (await session.execute(
                select(PgLink.from_note_id, PgLink.to_note_id, PgLink.link_type)
                .where(or_(PgLink.from_note_id == current, PgLink.to_note_id == current))
            )).all()
            for row in link_rows:
                from_id = row.from_note_id
                to_id = row.to_note_id
                neighbor = to_id if from_id == current else from_id
                edge_key = (from_id, to_id)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({"from_id": from_id, "to_id": to_id, "link_type": row.link_type})
                if neighbor not in visited:
                    visited[neighbor] = current_depth + 1
                    frontier.append(neighbor)

        if not visited:
            return {
                "root": root, "nodes": [root], "edges": [],
                "depth": depth, "node_count": 1, "edge_count": 0,
            }
        node_rows = (await session.execute(
            select(PgNote.id, PgNote.title, PgNote.para_category, PgNote.status)
            .where(PgNote.id.in_(list(visited.keys())))
        )).all()
        nodes = [
            {
                "id": r.id, "title": r.title, "para_category": r.para_category,
                "status": r.status, "depth": visited[r.id],
            }
            for r in node_rows
        ]

    logger.info("get_subgraph note=%s depth=%s nodes=%d edges=%d", note_id, depth, len(nodes), len(edges))
    return {
        "root": root, "nodes": nodes, "edges": edges,
        "depth": depth, "node_count": len(nodes), "edge_count": len(edges),
    }


async def _get_subgraph_sqlite(note_id: int, depth: int) -> dict:
    from app.database import get_connection
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, title, para_category, status FROM notes WHERE id = ?", (note_id,)
        )
        root_row = await cursor.fetchone()
        if root_row is None:
            return {
                "root": None, "nodes": [], "edges": [],
                "depth": depth, "node_count": 0, "edge_count": 0,
            }
        root = {
            "id": root_row["id"], "title": root_row["title"],
            "para_category": root_row["para_category"],
        }

        visited: dict[int, int] = {note_id: 0}
        edges: list[dict] = []
        seen_edges: set[tuple[int, int]] = set()
        frontier: deque[int] = deque([note_id])

        while frontier:
            current = frontier.popleft()
            current_depth = visited[current]
            if current_depth >= depth:
                continue
            cursor = await db.execute(
                "SELECT from_note_id, to_note_id, link_type FROM links "
                "WHERE from_note_id = ? OR to_note_id = ?",
                (current, current),
            )
            rows = await cursor.fetchall()
            for row in rows:
                from_id = row["from_note_id"]
                to_id = row["to_note_id"]
                neighbor = to_id if from_id == current else from_id
                edge_key = (from_id, to_id)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({"from_id": from_id, "to_id": to_id, "link_type": row["link_type"]})
                if neighbor not in visited:
                    visited[neighbor] = current_depth + 1
                    frontier.append(neighbor)

        node_ids = list(visited.keys())
        nodes = []
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            cursor = await db.execute(
                f"SELECT id, title, para_category, status FROM notes WHERE id IN ({placeholders})",
                node_ids,
            )
            note_rows = await cursor.fetchall()
            for r in note_rows:
                nodes.append({
                    "id": r["id"], "title": r["title"],
                    "para_category": r["para_category"], "status": r["status"],
                    "depth": visited[r["id"]],
                })

    logger.info("get_subgraph note=%s depth=%s nodes=%d edges=%d", note_id, depth, len(nodes), len(edges))
    return {
        "root": root, "nodes": nodes, "edges": edges,
        "depth": depth, "node_count": len(nodes), "edge_count": len(edges),
    }


# ── Path finding ────────────────────────────────────────────────────────────


async def find_path(from_id: int, to_id: int, max_depth: int = 5) -> dict | None:
    if settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL:
        return await _find_path_pg(from_id, to_id, max_depth)
    return await _find_path_sqlite(from_id, to_id, max_depth)


async def _find_path_pg(from_id: int, to_id: int, max_depth: int) -> dict | None:
    async with async_session_factory() as session:
        visited: set[int] = {from_id}
        queue: deque[list[int]] = deque([[from_id]])

        while queue:
            path = queue.popleft()
            current = path[-1]
            if len(path) - 1 >= max_depth:
                continue
            link_rows = (await session.execute(
                select(PgLink.from_note_id, PgLink.to_note_id, PgLink.link_type)
                .where(or_(PgLink.from_note_id == current, PgLink.to_note_id == current))
            )).all()
            for row in link_rows:
                neighbor = row.to_note_id if row.from_note_id == current else row.from_note_id
                if neighbor in visited:
                    continue
                new_path = path + [neighbor]
                if neighbor == to_id:
                    edge_list = []
                    for i in range(len(new_path) - 1):
                        a, b = new_path[i], new_path[i + 1]
                        erow = (await session.execute(
                            select(PgLink.from_note_id, PgLink.to_note_id, PgLink.link_type)
                            .where(or_(
                                (PgLink.from_note_id == a) & (PgLink.to_note_id == b),
                                (PgLink.from_note_id == b) & (PgLink.to_note_id == a),
                            ))
                        )).first()
                        if erow:
                            edge_list.append({
                                "from_id": erow.from_note_id, "to_id": erow.to_note_id,
                                "link_type": erow.link_type,
                            })
                    logger.info("find_path %s->%s length=%d", from_id, to_id, len(new_path) - 1)
                    return {"path": new_path, "edges": edge_list, "length": len(new_path) - 1}
                visited.add(neighbor)
                queue.append(new_path)
    logger.info("find_path %s->%s no path found", from_id, to_id)
    return None


async def _find_path_sqlite(from_id: int, to_id: int, max_depth: int) -> dict | None:
    from app.database import get_connection
    async with get_connection() as db:
        visited: set[int] = {from_id}
        queue: deque[list[int]] = deque([[from_id]])
        while queue:
            path = queue.popleft()
            current = path[-1]
            if len(path) - 1 >= max_depth:
                continue
            cursor = await db.execute(
                "SELECT from_note_id, to_note_id, link_type FROM links "
                "WHERE from_note_id = ? OR to_note_id = ?",
                (current, current),
            )
            rows = await cursor.fetchall()
            for row in rows:
                neighbor = row["to_note_id"] if row["from_note_id"] == current else row["from_note_id"]
                if neighbor in visited:
                    continue
                new_path = path + [neighbor]
                if neighbor == to_id:
                    edge_list = []
                    for i in range(len(new_path) - 1):
                        a, b = new_path[i], new_path[i + 1]
                        c = await db.execute(
                            "SELECT from_note_id, to_note_id, link_type FROM links "
                            "WHERE (from_note_id = ? AND to_note_id = ?) OR (from_note_id = ? AND to_note_id = ?)",
                            (a, b, b, a),
                        )
                        erow = await c.fetchone()
                        if erow:
                            edge_list.append({
                                "from_id": erow["from_note_id"], "to_id": erow["to_note_id"],
                                "link_type": erow["link_type"],
                            })
                    logger.info("find_path %s->%s length=%d", from_id, to_id, len(new_path) - 1)
                    return {"path": new_path, "edges": edge_list, "length": len(new_path) - 1}
                visited.add(neighbor)
                queue.append(new_path)
    logger.info("find_path %s->%s no path found", from_id, to_id)
    return None


# ── Connected components (clusters) ─────────────────────────────────────────


async def get_clusters(min_size: int = 2) -> list[dict]:
    if settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL:
        async with async_session_factory() as session:
            rows = (await session.execute(
                select(PgLink.from_note_id, PgLink.to_note_id)
            )).all()
            note_ids = list({x for pair in rows for x in (pair.from_note_id, pair.to_note_id)})
            if not note_ids:
                return []
            note_rows = (await session.execute(
                select(PgNote.id, PgNote.title).where(PgNote.id.in_(note_ids))
            )).all()
            titles = {r.id: r.title for r in note_rows}
    else:
        from app.database import get_connection
        async with get_connection() as db:
            cursor = await db.execute("SELECT from_note_id, to_note_id FROM links")
            rows = await cursor.fetchall()
            if not rows:
                return []
            cursor = await db.execute("SELECT id, title FROM notes")
            note_rows = await cursor.fetchall()
            titles = {r["id"]: r["title"] for r in note_rows}

    adjacency: dict[int, set[int]] = {}
    for pair in rows:
        a, b = (pair.from_note_id, pair.to_note_id) if not isinstance(pair, dict) else (pair["from_note_id"], pair["to_note_id"])
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    all_nodes = set(adjacency.keys())
    visited: set[int] = set()
    clusters: list[dict] = []
    cluster_id = 0

    for node in all_nodes:
        if node in visited:
            continue
        component: list[int] = []
        queue: deque[int] = deque([node])
        visited.add(node)
        while queue:
            current = queue.popleft()
            component.append(current)
            for neighbor in adjacency.get(current, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(neighbor)
        if len(component) >= min_size:
            clusters.append({
                "cluster_id": cluster_id,
                "note_ids": sorted(component),
                "size": len(component),
                "titles": [titles.get(nid, "") for nid in sorted(component)],
            })
            cluster_id += 1

    logger.info("get_clusters min_size=%d found=%d", min_size, len(clusters))
    return clusters


# ── Related notes (links + semantic fallback) ───────────────────────────────


async def get_related(note_id: int, limit: int = 10) -> list[dict]:
    results: list[dict] = []
    seen_ids: set[int] = {note_id}

    if settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL:
        async with async_session_factory() as session:
            link_rows = (await session.execute(
                select(PgLink.from_note_id, PgLink.to_note_id, PgLink.link_type)
                .where(or_(PgLink.from_note_id == note_id, PgLink.to_note_id == note_id))
            )).all()
            linked_ids: list[tuple[int, str]] = []
            for row in link_rows:
                neighbor = row.to_note_id if row.from_note_id == note_id else row.from_note_id
                if neighbor not in seen_ids:
                    seen_ids.add(neighbor)
                    linked_ids.append((neighbor, row.link_type))
            if linked_ids:
                ids_only = [lid for lid, _ in linked_ids]
                note_rows = (await session.execute(
                    select(PgNote.id, PgNote.title, PgNote.para_category)
                    .where(PgNote.id.in_(ids_only))
                )).all()
                note_map = {r.id: r for r in note_rows}
                link_type_map = {lid: lt for lid, lt in linked_ids}
                for nid, _ in linked_ids:
                    if nid in note_map and len(results) < limit:
                        r = note_map[nid]
                        results.append({
                            "id": r.id, "title": r.title, "para_category": r.para_category,
                            "relation": "linked", "link_type": link_type_map.get(nid),
                        })
    else:
        from app.database import get_connection
        async with get_connection() as db:
            cursor = await db.execute(
                "SELECT from_note_id, to_note_id, link_type FROM links "
                "WHERE from_note_id = ? OR to_note_id = ?",
                (note_id, note_id),
            )
            rows = await cursor.fetchall()
            linked_ids = []
            for row in rows:
                neighbor = row["to_note_id"] if row["from_note_id"] == note_id else row["from_note_id"]
                if neighbor not in seen_ids:
                    seen_ids.add(neighbor)
                    linked_ids.append((neighbor, row["link_type"]))
            if linked_ids:
                placeholders = ",".join("?" for _ in linked_ids)
                ids_only = [lid for lid, _ in linked_ids]
                c = await db.execute(
                    f"SELECT id, title, para_category FROM notes WHERE id IN ({placeholders})",
                    ids_only,
                )
                note_rows = await c.fetchall()
                note_map = {r["id"]: r for r in note_rows}
                link_type_map = {lid: lt for lid, lt in linked_ids}
                for nid, _ in linked_ids:
                    if nid in note_map and len(results) < limit:
                        r = note_map[nid]
                        results.append({
                            "id": r["id"], "title": r["title"],
                            "para_category": r["para_category"],
                            "relation": "linked", "link_type": link_type_map.get(nid),
                        })

    if len(results) < limit:
        try:
            from app.embed import embed_text
            from app.vector_store import semantic_search
            from sqlalchemy import text as sa_text

            if settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL:
                async with async_session_factory() as session:
                    note_row = (await session.execute(
                        select(PgNote.title, PgNote.content).where(PgNote.id == note_id)
                    )).first()
                    if note_row:
                        embedding = await embed_text(f"{note_row.title} {note_row.content}")
                        if embedding:
                            sem_results = await semantic_search(session, embedding, limit=limit)
                            for sem_id, _score in sem_results:
                                if sem_id not in seen_ids and len(results) < limit:
                                    sr = (await session.execute(
                                        select(PgNote.id, PgNote.title, PgNote.para_category)
                                        .where(PgNote.id == sem_id)
                                    )).first()
                                    if sr:
                                        seen_ids.add(sem_id)
                                        results.append({
                                            "id": sr.id, "title": sr.title,
                                            "para_category": sr.para_category,
                                            "relation": "semantic", "link_type": None,
                                        })
            else:
                from app.database import get_connection
                async with get_connection() as db:
                    cursor = await db.execute("SELECT title, content FROM notes WHERE id = ?", (note_id,))
                    note_row = await cursor.fetchone()
                    if note_row:
                        embedding = await embed_text(f"{note_row['title']} {note_row['content']}")
                        if embedding:
                            sem_results = await semantic_search(db, embedding, limit=limit)
                            for sem_id, _score in sem_results:
                                if sem_id not in seen_ids and len(results) < limit:
                                    c = await db.execute(
                                        "SELECT id, title, para_category FROM notes WHERE id = ?",
                                        (sem_id,),
                                    )
                                    sr = await c.fetchone()
                                    if sr:
                                        seen_ids.add(sem_id)
                                        results.append({
                                            "id": sr["id"], "title": sr["title"],
                                            "para_category": sr["para_category"],
                                            "relation": "semantic", "link_type": None,
                                        })
        except Exception:
            logger.warning("Semantic related search failed for note %s, using links only", note_id, exc_info=True)

    logger.info("get_related note=%s results=%d", note_id, len(results))
    return results


# ── Cross-category links ────────────────────────────────────────────────────


async def get_cross_category_links(note_id: int) -> list[dict]:
    if settings.PARA_DB_URL and "postgresql" in settings.PARA_DB_URL:
        async with async_session_factory() as session:
            root_row = (await session.execute(
                select(PgNote.para_category).where(PgNote.id == note_id)
            )).first()
            if root_row is None:
                return []
            root_category = root_row.para_category
            link_rows = (await session.execute(
                select(PgLink.from_note_id, PgLink.to_note_id, PgLink.link_type)
                .where(or_(PgLink.from_note_id == note_id, PgLink.to_note_id == note_id))
            )).all()
            neighbor_ids = []
            for row in link_rows:
                neighbor = row.to_note_id if row.from_note_id == note_id else row.from_note_id
                neighbor_ids.append((neighbor, row.link_type))
            results = []
            for nid, link_type in neighbor_ids:
                nr = (await session.execute(
                    select(PgNote.id, PgNote.title, PgNote.para_category).where(PgNote.id == nid)
                )).first()
                if nr and nr.para_category != root_category:
                    results.append({
                        "id": nr.id, "title": nr.title,
                        "para_category": nr.para_category, "link_type": link_type,
                    })
            logger.info("get_cross_category_links note=%s results=%d", note_id, len(results))
            return results

    from app.database import get_connection
    async with get_connection() as db:
        cursor = await db.execute("SELECT para_category FROM notes WHERE id = ?", (note_id,))
        root_row = await cursor.fetchone()
        if root_row is None:
            return []
        root_category = root_row["para_category"]
        cursor = await db.execute(
            "SELECT from_note_id, to_note_id, link_type FROM links "
            "WHERE from_note_id = ? OR to_note_id = ?",
            (note_id, note_id),
        )
        rows = await cursor.fetchall()
        neighbor_ids = []
        for row in rows:
            neighbor = row["to_note_id"] if row["from_note_id"] == note_id else row["from_note_id"]
            neighbor_ids.append((neighbor, row["link_type"]))
        results = []
        for nid, link_type in neighbor_ids:
            c = await db.execute(
                "SELECT id, title, para_category FROM notes WHERE id = ?",
                (nid,),
            )
            nr = await c.fetchone()
            if nr and nr["para_category"] != root_category:
                results.append({
                    "id": nr["id"], "title": nr["title"],
                    "para_category": nr["para_category"], "link_type": link_type,
                })
        logger.info("get_cross_category_links note=%s results=%d", note_id, len(results))
        return results

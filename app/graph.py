"""Knowledge graph reasoning over the links table (SB-05)."""

from __future__ import annotations

import logging
from collections import deque

from app.database import get_connection

logger = logging.getLogger("para.graph")


async def get_subgraph(note_id: int, depth: int = 2) -> dict:
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, title, para_category, status FROM notes WHERE id = ?",
            (note_id,),
        )
        root_row = await cursor.fetchone()
        if root_row is None:
            return {
                "root": None,
                "nodes": [],
                "edges": [],
                "depth": depth,
                "node_count": 0,
                "edge_count": 0,
            }

        root = {
            "id": root_row["id"],
            "title": root_row["title"],
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
                    edges.append({
                        "from_id": from_id,
                        "to_id": to_id,
                        "link_type": row["link_type"],
                    })

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
                    "id": r["id"],
                    "title": r["title"],
                    "para_category": r["para_category"],
                    "status": r["status"],
                    "depth": visited[r["id"]],
                })

        logger.info("get_subgraph note=%s depth=%s nodes=%d edges=%d", note_id, depth, len(nodes), len(edges))

        return {
            "root": root,
            "nodes": nodes,
            "edges": edges,
            "depth": depth,
            "node_count": len(nodes),
            "edge_count": len(edges),
        }


async def find_path(from_id: int, to_id: int, max_depth: int = 5) -> dict | None:
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
                                "from_id": erow["from_note_id"],
                                "to_id": erow["to_note_id"],
                                "link_type": erow["link_type"],
                            })
                    logger.info("find_path %s->%s length=%d", from_id, to_id, len(new_path) - 1)
                    return {"path": new_path, "edges": edge_list, "length": len(new_path) - 1}

                visited.add(neighbor)
                queue.append(new_path)

        logger.info("find_path %s->%s no path found", from_id, to_id)
        return None


async def get_clusters(min_size: int = 2) -> list[dict]:
    async with get_connection() as db:
        cursor = await db.execute("SELECT from_note_id, to_note_id FROM links")
        rows = await cursor.fetchall()

        adjacency: dict[int, set[int]] = {}
        for row in rows:
            f, t = row["from_note_id"], row["to_note_id"]
            adjacency.setdefault(f, set()).add(t)
            adjacency.setdefault(t, set()).add(f)

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
                placeholders = ",".join("?" for _ in component)
                c = await db.execute(
                    f"SELECT id, title FROM notes WHERE id IN ({placeholders})",
                    component,
                )
                note_rows = await c.fetchall()
                titles = {r["id"]: r["title"] for r in note_rows}
                clusters.append({
                    "cluster_id": cluster_id,
                    "note_ids": sorted(component),
                    "size": len(component),
                    "titles": [titles.get(nid, "") for nid in sorted(component)],
                })
                cluster_id += 1

        logger.info("get_clusters min_size=%d found=%d", min_size, len(clusters))
        return clusters


async def get_related(note_id: int, limit: int = 10) -> list[dict]:
    results: list[dict] = []
    seen_ids: set[int] = {note_id}

    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT from_note_id, to_note_id, link_type FROM links "
            "WHERE from_note_id = ? OR to_note_id = ?",
            (note_id, note_id),
        )
        rows = await cursor.fetchall()

        linked_ids: list[tuple[int, str]] = []
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
                        "id": r["id"],
                        "title": r["title"],
                        "para_category": r["para_category"],
                        "relation": "linked",
                        "link_type": link_type_map.get(nid),
                    })

    if len(results) < limit:
        try:
            from app.embed import embed_text
            from app.vector_store import semantic_search

            async with get_connection() as db:
                cursor = await db.execute("SELECT title, content FROM notes WHERE id = ?", (note_id,))
                note_row = await cursor.fetchone()
                if note_row:
                    embedding = await embed_text(f"{note_row['title']} {note_row['content']}")
                    if embedding:
                        sem_results = await semantic_search(db, embedding, limit=limit)
                        for sem_id, score in sem_results:
                            if sem_id not in seen_ids and len(results) < limit:
                                c = await db.execute(
                                    "SELECT id, title, para_category FROM notes WHERE id = ?",
                                    (sem_id,),
                                )
                                sr = await c.fetchone()
                                if sr:
                                    seen_ids.add(sem_id)
                                    results.append({
                                        "id": sr["id"],
                                        "title": sr["title"],
                                        "para_category": sr["para_category"],
                                        "relation": "semantic",
                                        "link_type": None,
                                    })
        except Exception:
            logger.warning("Semantic related search failed for note %s, using links only", note_id, exc_info=True)

    logger.info("get_related note=%s results=%d", note_id, len(results))
    return results


async def get_cross_category_links(note_id: int) -> list[dict]:
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

        neighbor_ids: list[tuple[int, str]] = []
        for row in rows:
            neighbor = row["to_note_id"] if row["from_note_id"] == note_id else row["from_note_id"]
            neighbor_ids.append((neighbor, row["link_type"]))

        results: list[dict] = []
        for nid, link_type in neighbor_ids:
            c = await db.execute(
                "SELECT id, title, para_category FROM notes WHERE id = ?",
                (nid,),
            )
            nr = await c.fetchone()
            if nr and nr["para_category"] != root_category:
                results.append({
                    "id": nr["id"],
                    "title": nr["title"],
                    "para_category": nr["para_category"],
                    "link_type": link_type,
                })

        logger.info("get_cross_category_links note=%s results=%d", note_id, len(results))
        return results

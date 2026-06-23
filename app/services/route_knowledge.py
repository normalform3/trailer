from __future__ import annotations

import json
import os
import re
import sqlite3
from uuid import uuid4
from difflib import SequenceMatcher
from pathlib import Path
from threading import Lock
from typing import Protocol

from app.config import PROJECT_ROOT
from app.models import (
    RecommendationEvidence,
    RouteImportCandidateRecord,
    RouteImportExtractedCandidate,
    RouteImportJobRecord,
    RouteKnowledgeCreate,
    RouteKnowledgeListResponse,
    RouteKnowledgeRecord,
    RouteKnowledgeSource,
    RouteKnowledgeUpdate,
    RouteRecommendationIntent,
)
from app.providers.route_discovery import RawRouteCandidate
from app.services.route_knowledge_seed import starter_routes


class RouteKnowledgeRepository(Protocol):
    def search(self, intent: RouteRecommendationIntent, limit: int = 8) -> list[RawRouteCandidate]:
        raise NotImplementedError


class NullRouteKnowledgeRepository:
    def search(self, intent: RouteRecommendationIntent, limit: int = 8) -> list[RawRouteCandidate]:
        return []


class SQLiteRouteKnowledgeRepository:
    def __init__(self, database_path: Path | str | None = None) -> None:
        configured = os.getenv("ROUTE_KNOWLEDGE_DB")
        self.database_path = Path(database_path or configured or PROJECT_ROOT / "data" / "route_knowledge.sqlite3")
        self._initialized = False
        self._fts_enabled = False
        self._lock = Lock()

    def search(self, intent: RouteRecommendationIntent, limit: int = 8) -> list[RawRouteCandidate]:
        self._initialize()
        with self._connect() as connection:
            pool_limit = max(limit * 5, 30) if intent.destination_region else max(limit * 20, 150)
            rows = self._candidate_rows(connection, intent, pool_limit)
            candidates = [self._to_candidate(connection, row) for row in rows]
        candidates = [candidate for candidate in candidates if not _hard_conflict(intent, candidate)]
        candidates.sort(key=lambda item: (-_knowledge_score(intent, item), -item.editorial_rank, item.name))
        return candidates[:limit]

    def list_records(
        self,
        query: str | None = None,
        status: str | None = None,
        province: str | None = None,
        city: str | None = None,
        source_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> RouteKnowledgeListResponse:
        self._initialize()
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        where, params = self._management_filters(query, status, province, city, source_type)
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM routes {where}", params).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT routes.* FROM routes
                {where}
                ORDER BY routes.status = 'active' DESC, routes.editorial_rank DESC, routes.name
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
            records = [self._to_record(connection, row) for row in rows]
        return RouteKnowledgeListResponse(records=records, total=total, limit=limit, offset=offset)

    def get_record(self, route_id: str) -> RouteKnowledgeRecord | None:
        self._initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
            return self._to_record(connection, row) if row else None

    def create_record(self, payload: RouteKnowledgeCreate) -> RouteKnowledgeRecord:
        self._initialize()
        route_id = _route_id(payload)
        with self._lock:
            with self._connect() as connection:
                if connection.execute("SELECT 1 FROM routes WHERE id = ?", (route_id,)).fetchone():
                    raise ValueError(f"路线知识条目已存在：{route_id}")
                self._write_route(connection, route_id, payload)
                self._replace_related(connection, route_id, payload)
                self._sync_fts(connection, route_id)
                row = connection.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
                return self._to_record(connection, row)

    def update_record(self, route_id: str, payload: RouteKnowledgeUpdate) -> RouteKnowledgeRecord | None:
        self._initialize()
        with self._lock:
            with self._connect() as connection:
                if not connection.execute("SELECT 1 FROM routes WHERE id = ?", (route_id,)).fetchone():
                    return None
                self._write_route(connection, route_id, payload)
                self._replace_related(connection, route_id, payload)
                self._sync_fts(connection, route_id)
                row = connection.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
                return self._to_record(connection, row)

    def archive_record(self, route_id: str) -> RouteKnowledgeRecord | None:
        self._initialize()
        with self._lock:
            with self._connect() as connection:
                if not connection.execute("SELECT 1 FROM routes WHERE id = ?", (route_id,)).fetchone():
                    return None
                connection.execute("UPDATE routes SET status = 'archived' WHERE id = ?", (route_id,))
                self._delete_fts(connection, route_id)
                row = connection.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
                return self._to_record(connection, row)

    def delete_record(self, route_id: str) -> bool:
        self._initialize()
        with self._lock:
            with self._connect() as connection:
                self._delete_fts(connection, route_id)
                cursor = connection.execute("DELETE FROM routes WHERE id = ?", (route_id,))
                return cursor.rowcount > 0

    def create_import_job(
        self,
        source_url: str | None,
        raw_text: str,
        title: str | None,
        warnings: list[str],
        candidates: list[RouteImportExtractedCandidate],
    ) -> RouteImportJobRecord:
        self._initialize()
        job_id = f"import-{uuid4().hex[:12]}"
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO route_import_jobs (id, source_url, raw_text, title, status, created_at, warnings)
                    VALUES (?, ?, ?, ?, ?, datetime('now'), ?)
                    """,
                    (
                        job_id,
                        source_url,
                        raw_text,
                        title,
                        "ready" if candidates else "needs_review",
                        json.dumps(warnings, ensure_ascii=False),
                    ),
                )
                for candidate in candidates:
                    match = self._match_import_candidate(connection, candidate)
                    action = "merge" if match["score"] >= 92 else "needs_review" if match["score"] >= 70 else "create"
                    province, city = _candidate_region_parts(candidate)
                    connection.execute(
                        """
                        INSERT INTO route_import_candidates (
                            job_id, extracted_name, region, province, city, summary, tags, distance_km,
                            duration_hours, ascent_m, risk_notes, source_title, source_url, source_type,
                            matched_route_id, matched_route_name, match_score, suggested_action, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            candidate.name,
                            candidate.region,
                            province,
                            city,
                            candidate.summary,
                            json.dumps(candidate.tags, ensure_ascii=False),
                            candidate.distance_km,
                            candidate.duration_hours,
                            candidate.ascent_m,
                            json.dumps(candidate.risk_notes, ensure_ascii=False),
                            title or "导入资料",
                            source_url,
                            "imported-document",
                            match["route_id"],
                            match["route_name"],
                            match["score"],
                            action,
                            "pending" if action != "needs_review" else "needs_review",
                        ),
                    )
        created = self.get_import_job(job_id)
        if created is None:
            raise RuntimeError("导入任务创建失败")
        return created

    def get_import_job(self, job_id: str) -> RouteImportJobRecord | None:
        self._initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM route_import_jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            candidates = [
                self._to_import_candidate(item)
                for item in connection.execute(
                    "SELECT * FROM route_import_candidates WHERE job_id = ? ORDER BY id",
                    (job_id,),
                )
            ]
            return RouteImportJobRecord(
                id=row["id"],
                source_url=row["source_url"],
                title=row["title"],
                status=row["status"],
                created_at=row["created_at"],
                warnings=json.loads(row["warnings"] or "[]"),
                candidates=candidates,
            )

    def get_import_candidate(self, candidate_id: int) -> RouteImportCandidateRecord | None:
        self._initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM route_import_candidates WHERE id = ?", (candidate_id,)).fetchone()
            return self._to_import_candidate(row) if row else None

    def update_import_candidate_status(
        self,
        candidate_id: int,
        status: str,
        action: str | None = None,
        target_route_id: str | None = None,
    ) -> None:
        self._initialize()
        with self._connect() as connection:
            updates = ["status = ?"]
            params: list[object] = [status]
            if action:
                updates.append("suggested_action = ?")
                params.append(action)
            if target_route_id:
                record = self.get_record(target_route_id)
                updates.extend(["matched_route_id = ?", "matched_route_name = ?"])
                params.extend([target_route_id, record.name if record else None])
            params.append(candidate_id)
            connection.execute(f"UPDATE route_import_candidates SET {', '.join(updates)} WHERE id = ?", params)

    def update_import_job_status(self, job_id: str, status: str, warnings: list[str] | None = None) -> None:
        self._initialize()
        with self._connect() as connection:
            if warnings is None:
                connection.execute("UPDATE route_import_jobs SET status = ? WHERE id = ?", (status, job_id))
            else:
                connection.execute(
                    "UPDATE route_import_jobs SET status = ?, warnings = ? WHERE id = ?",
                    (status, json.dumps(warnings, ensure_ascii=False), job_id),
                )

    def import_candidate_to_payload(self, candidate: RouteImportCandidateRecord) -> RouteKnowledgeCreate:
        province, city = _candidate_region_parts(candidate)
        source = []
        if candidate.source_url:
            source.append(
                RouteKnowledgeSource(
                    title=candidate.source_title or "导入资料",
                    url=candidate.source_url,
                    source_type=candidate.source_type,
                    summary=candidate.summary,
                )
            )
        return RouteKnowledgeCreate(
            name=candidate.name,
            province=province or "待核验",
            city=city or "待核验",
            summary=candidate.summary,
            distance_km=candidate.distance_km,
            duration_hours=candidate.duration_hours,
            ascent_m=candidate.ascent_m,
            tags=candidate.tags,
            risk_notes=[*candidate.risk_notes, "导入资料生成的路线卡需人工核验。"],
            status="active",
            sources=source,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                self._migrate_schema(connection)
                try:
                    connection.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS route_fts USING fts5(route_id UNINDEXED, name, region, aliases, tags)"
                    )
                    self._fts_enabled = True
                except sqlite3.OperationalError:
                    self._fts_enabled = False
                if connection.execute("SELECT COUNT(*) FROM routes").fetchone()[0] == 0:
                    self._seed(connection)
            self._initialized = True

    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(routes)")}
        if "seasons" not in columns:
            connection.execute("ALTER TABLE routes ADD COLUMN seasons TEXT NOT NULL DEFAULT '[]'")
        if "summary" not in columns:
            connection.execute("ALTER TABLE routes ADD COLUMN summary TEXT")
        if "transport_notes" not in columns:
            connection.execute("ALTER TABLE routes ADD COLUMN transport_notes TEXT NOT NULL DEFAULT '[]'")
        source_columns = {row[1] for row in connection.execute("PRAGMA table_info(route_sources)")}
        if "summary" not in source_columns:
            connection.execute("ALTER TABLE route_sources ADD COLUMN summary TEXT")

    def _management_filters(
        self,
        query: str | None,
        status: str | None,
        province: str | None,
        city: str | None,
        source_type: str | None,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        cleaned_query = (query or "").strip()
        if cleaned_query:
            clauses.append(
                """(
                    routes.name LIKE ? OR routes.province LIKE ? OR routes.city LIKE ? OR
                    routes.summary LIKE ? OR
                    EXISTS (SELECT 1 FROM route_aliases WHERE route_aliases.route_id = routes.id AND route_aliases.alias LIKE ?) OR
                    EXISTS (SELECT 1 FROM route_tags WHERE route_tags.route_id = routes.id AND route_tags.tag LIKE ?)
                )"""
            )
            like = f"%{cleaned_query}%"
            params.extend([like, like, like, like, like, like])
        if status:
            clauses.append("routes.status = ?")
            params.append(status)
        if province:
            clauses.append("routes.province LIKE ?")
            params.append(f"%{province.strip()}%")
        if city:
            clauses.append("routes.city LIKE ?")
            params.append(f"%{city.strip()}%")
        if source_type:
            clauses.append("EXISTS (SELECT 1 FROM route_sources WHERE route_sources.route_id = routes.id AND route_sources.source_type = ?)")
            params.append(source_type.strip())
        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)

    def _write_route(
        self,
        connection: sqlite3.Connection,
        route_id: str,
        payload: RouteKnowledgeCreate | RouteKnowledgeUpdate,
    ) -> None:
        connection.execute(
            """
            INSERT INTO routes (
                id, name, province, city, summary, difficulty, distance_km, duration_hours, ascent_m,
                camping, seasons, transport_notes, editorial_rank, official_status, risk_level,
                risk_notes, status, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                province = excluded.province,
                city = excluded.city,
                summary = excluded.summary,
                difficulty = excluded.difficulty,
                distance_km = excluded.distance_km,
                duration_hours = excluded.duration_hours,
                ascent_m = excluded.ascent_m,
                camping = excluded.camping,
                seasons = excluded.seasons,
                transport_notes = excluded.transport_notes,
                editorial_rank = excluded.editorial_rank,
                official_status = excluded.official_status,
                risk_level = excluded.risk_level,
                risk_notes = excluded.risk_notes,
                status = excluded.status,
                last_verified_at = excluded.last_verified_at
            """,
            (
                route_id, payload.name, payload.province, payload.city, payload.summary, payload.difficulty,
                payload.distance_km, payload.duration_hours, payload.ascent_m,
                None if payload.camping is None else int(payload.camping),
                json.dumps(payload.seasons, ensure_ascii=False),
                json.dumps(payload.transport_notes, ensure_ascii=False),
                payload.editorial_rank, payload.official_status, payload.risk_level,
                json.dumps(payload.risk_notes, ensure_ascii=False), payload.status, payload.last_verified_at,
            ),
        )

    def _replace_related(
        self,
        connection: sqlite3.Connection,
        route_id: str,
        payload: RouteKnowledgeCreate | RouteKnowledgeUpdate,
    ) -> None:
        connection.execute("DELETE FROM route_aliases WHERE route_id = ?", (route_id,))
        connection.execute("DELETE FROM route_tags WHERE route_id = ?", (route_id,))
        connection.execute("DELETE FROM route_sources WHERE route_id = ?", (route_id,))
        for alias in payload.aliases:
            connection.execute("INSERT OR IGNORE INTO route_aliases (route_id, alias) VALUES (?, ?)", (route_id, alias))
        for tag in payload.tags:
            connection.execute("INSERT OR IGNORE INTO route_tags (route_id, tag) VALUES (?, ?)", (route_id, tag))
        for source in payload.sources:
            connection.execute(
                """
                INSERT OR IGNORE INTO route_sources (route_id, title, url, source_type, published_at, summary)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (route_id, source.title, source.url, source.source_type, source.published_at, source.summary),
            )

    def _sync_fts(self, connection: sqlite3.Connection, route_id: str) -> None:
        if not self._fts_enabled:
            return
        self._delete_fts(connection, route_id)
        row = connection.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if row is None or row["status"] != "active":
            return
        aliases = [item[0] for item in connection.execute("SELECT alias FROM route_aliases WHERE route_id = ?", (route_id,))]
        tags = [item[0] for item in connection.execute("SELECT tag FROM route_tags WHERE route_id = ?", (route_id,))]
        try:
            connection.execute(
                "INSERT INTO route_fts (route_id, name, region, aliases, tags) VALUES (?, ?, ?, ?, ?)",
                (route_id, row["name"], f"{row['province']} {row['city']}", " ".join(aliases), " ".join(tags)),
            )
        except sqlite3.OperationalError:
            self._fts_enabled = False

    def _delete_fts(self, connection: sqlite3.Connection, route_id: str) -> None:
        if not self._fts_enabled:
            return
        try:
            connection.execute("DELETE FROM route_fts WHERE route_id = ?", (route_id,))
        except sqlite3.OperationalError:
            self._fts_enabled = False

    def _seed(self, connection: sqlite3.Connection) -> None:
        for record in starter_routes():
            connection.execute(
                """
                INSERT INTO routes (
                    id, name, province, city, summary, difficulty, distance_km, duration_hours, ascent_m,
                    camping, seasons, transport_notes, editorial_rank, official_status, risk_level, risk_notes,
                    status, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"], record["name"], record["province"], record["city"], record.get("summary"),
                    record.get("difficulty"),
                    record.get("distance_km"), record.get("duration_hours"), record.get("ascent_m"),
                    record.get("camping"), json.dumps(record["seasons"], ensure_ascii=False),
                    json.dumps(record.get("transport_notes", []), ensure_ascii=False),
                    record["editorial_rank"], record["official_status"], record["risk_level"],
                    json.dumps(record["risk_notes"], ensure_ascii=False), record["status"], record["last_verified_at"],
                ),
            )
            for alias in record["aliases"]:
                connection.execute("INSERT INTO route_aliases (route_id, alias) VALUES (?, ?)", (record["id"], alias))
            for tag in record["tags"]:
                connection.execute("INSERT INTO route_tags (route_id, tag) VALUES (?, ?)", (record["id"], tag))
            for source in record["sources"]:
                connection.execute(
                    "INSERT INTO route_sources (route_id, title, url, source_type, published_at, summary) VALUES (?, ?, ?, ?, ?, ?)",
                    (record["id"], source["title"], source["url"], source["source_type"], source.get("published_at"), source.get("summary")),
                )
            if self._fts_enabled:
                connection.execute(
                    "INSERT INTO route_fts (route_id, name, region, aliases, tags) VALUES (?, ?, ?, ?, ?)",
                    (
                        record["id"], record["name"], f"{record['province']} {record['city']}",
                        " ".join(record["aliases"]), " ".join(record["tags"]),
                    ),
                )

    def _candidate_rows(
        self,
        connection: sqlite3.Connection,
        intent: RouteRecommendationIntent,
        limit: int,
    ) -> list[sqlite3.Row]:
        region = _normalize_region(intent.destination_region)
        terms = [region, *intent.scenery_preferences]
        terms = [term.strip() for term in terms if term and term.strip() and term not in {"中国", "全国", "全国均可"}]
        if self._fts_enabled and terms:
            query = " OR ".join(f'"{_fts_term(term)}"' for term in terms if _fts_term(term))
            if query:
                try:
                    sql = """
                        SELECT routes.* FROM route_fts
                        JOIN routes ON routes.id = route_fts.route_id
                        WHERE route_fts MATCH ? AND routes.status = 'active'
                    """
                    params: list[object] = [query]
                    if region:
                        sql += """ AND (
                            routes.province LIKE ? OR routes.city LIKE ? OR routes.name LIKE ? OR
                            EXISTS (SELECT 1 FROM route_aliases WHERE route_aliases.route_id = routes.id AND route_aliases.alias LIKE ?)
                        )"""
                        params.extend([f"%{region}%", f"%{region}%", f"%{region}%", f"%{region}%"])
                    sql += " ORDER BY bm25(route_fts), routes.editorial_rank DESC LIMIT ?"
                    params.append(limit)
                    rows = connection.execute(
                        sql,
                        params,
                    ).fetchall()
                    if rows:
                        return rows
                except sqlite3.OperationalError:
                    self._fts_enabled = False

        sql = "SELECT DISTINCT routes.* FROM routes LEFT JOIN route_tags ON route_tags.route_id = routes.id WHERE routes.status = 'active'"
        params: list[object] = []
        if region:
            sql += """ AND (
                routes.province LIKE ? OR routes.city LIKE ? OR routes.name LIKE ? OR
                EXISTS (SELECT 1 FROM route_aliases WHERE route_aliases.route_id = routes.id AND route_aliases.alias LIKE ?)
            )"""
            params.extend([f"%{region}%", f"%{region}%", f"%{region}%", f"%{region}%"])
        if intent.scenery_preferences:
            clauses = []
            for preference in intent.scenery_preferences:
                clauses.append("route_tags.tag LIKE ?")
                params.append(f"%{preference}%")
            sql += f" AND ({' OR '.join(clauses)})"
        sql += " ORDER BY routes.editorial_rank DESC, routes.name LIMIT ?"
        params.append(limit)
        return connection.execute(sql, params).fetchall()

    def _to_candidate(self, connection: sqlite3.Connection, row: sqlite3.Row) -> RawRouteCandidate:
        route_id = row["id"]
        aliases = [item[0] for item in connection.execute("SELECT alias FROM route_aliases WHERE route_id = ?", (route_id,))]
        tags = [item[0] for item in connection.execute("SELECT tag FROM route_tags WHERE route_id = ?", (route_id,))]
        sources = [
            RecommendationEvidence(title=item[0], url=item[1], source_type=item[2], published_at=item[3], summary=item[4])
            for item in connection.execute(
                "SELECT title, url, source_type, published_at, summary FROM route_sources WHERE route_id = ? ORDER BY source_type = 'official' DESC, id",
                (route_id,),
            )
        ]
        risk_notes = json.loads(row["risk_notes"] or "[]")
        seasons = json.loads(row["seasons"] or "[]")
        transport_notes = json.loads(row["transport_notes"] or "[]")
        return RawRouteCandidate(
            name=row["name"], aliases=aliases, region=_format_region(row["province"], row["city"]),
            summary=row["summary"], difficulty=row["difficulty"], distance_km=row["distance_km"], duration_hours=row["duration_hours"],
            ascent_m=row["ascent_m"], scenery=tags, seasons=seasons, transport_notes=transport_notes,
            camping=None if row["camping"] is None else bool(row["camping"]),
            evidence=sources, verification_items=risk_notes, retrieval_source="knowledge_base",
            popularity_label=_popularity_label(row["editorial_rank"], row["official_status"]),
            last_verified_at=row["last_verified_at"], official_status=row["official_status"],
            editorial_rank=row["editorial_rank"], risk_level=row["risk_level"],
        )

    def _to_record(self, connection: sqlite3.Connection, row: sqlite3.Row) -> RouteKnowledgeRecord:
        route_id = row["id"]
        aliases = [item[0] for item in connection.execute("SELECT alias FROM route_aliases WHERE route_id = ? ORDER BY id", (route_id,))]
        tags = [item[0] for item in connection.execute("SELECT tag FROM route_tags WHERE route_id = ? ORDER BY id", (route_id,))]
        sources = [
            RouteKnowledgeSource(
                id=item[0],
                title=item[1],
                url=item[2],
                source_type=item[3],
                published_at=item[4],
                summary=item[5],
            )
            for item in connection.execute(
                """
                SELECT id, title, url, source_type, published_at, summary
                FROM route_sources
                WHERE route_id = ?
                ORDER BY source_type = 'official' DESC, id
                """,
                (route_id,),
            )
        ]
        seasons = json.loads(row["seasons"] or "[]")
        transport_notes = json.loads(row["transport_notes"] or "[]")
        risk_notes = json.loads(row["risk_notes"] or "[]")
        return RouteKnowledgeRecord(
            id=route_id,
            name=row["name"],
            province=row["province"],
            city=row["city"],
            region=_format_region(row["province"], row["city"]),
            summary=row["summary"],
            difficulty=row["difficulty"],
            distance_km=row["distance_km"],
            duration_hours=row["duration_hours"],
            ascent_m=row["ascent_m"],
            camping=None if row["camping"] is None else bool(row["camping"]),
            seasons=seasons,
            aliases=aliases,
            tags=tags,
            transport_notes=transport_notes,
            editorial_rank=row["editorial_rank"],
            official_status=row["official_status"],
            risk_level=row["risk_level"],
            risk_notes=risk_notes,
            status=row["status"],
            last_verified_at=row["last_verified_at"],
            sources=sources,
            source_count=len(sources),
        )

    def _to_import_candidate(self, row: sqlite3.Row) -> RouteImportCandidateRecord:
        return RouteImportCandidateRecord(
            id=row["id"],
            job_id=row["job_id"],
            name=row["extracted_name"],
            region=row["region"],
            province=row["province"],
            city=row["city"],
            summary=row["summary"],
            tags=json.loads(row["tags"] or "[]"),
            distance_km=row["distance_km"],
            duration_hours=row["duration_hours"],
            ascent_m=row["ascent_m"],
            risk_notes=json.loads(row["risk_notes"] or "[]"),
            source_title=row["source_title"],
            source_url=row["source_url"],
            source_type=row["source_type"],
            matched_route_id=row["matched_route_id"],
            matched_route_name=row["matched_route_name"],
            match_score=row["match_score"],
            suggested_action=row["suggested_action"],
            status=row["status"],
        )

    def _match_import_candidate(
        self,
        connection: sqlite3.Connection,
        candidate: RouteImportExtractedCandidate,
    ) -> dict[str, object]:
        candidate_name = _normalized_name(candidate.name)
        candidate_region = _normalize_region(candidate.region or " ".join(item for item in [candidate.province, candidate.city] if item))
        candidate_tags = {_normalized_name(tag) for tag in candidate.tags if tag}
        best: dict[str, object] = {"route_id": None, "route_name": None, "score": 0}
        rows = connection.execute("SELECT * FROM routes WHERE status != 'archived'").fetchall()
        for row in rows:
            route_id = row["id"]
            aliases = [
                item[0]
                for item in connection.execute("SELECT alias FROM route_aliases WHERE route_id = ?", (route_id,))
            ]
            tags = [
                item[0]
                for item in connection.execute("SELECT tag FROM route_tags WHERE route_id = ?", (route_id,))
            ]
            names = [_normalized_name(row["name"]), *(_normalized_name(alias) for alias in aliases)]
            route_region = _normalize_region(_format_region(row["province"], row["city"]))
            same_region = bool(candidate_region and (candidate_region in route_region or route_region in candidate_region))
            same_name = candidate_name in names
            similarity = max((SequenceMatcher(None, candidate_name, name).ratio() for name in names if name), default=0)
            tag_overlap = len(candidate_tags.intersection({_normalized_name(tag) for tag in tags if tag}))
            score = 0
            if same_name and same_region:
                score = 100
            elif same_name:
                score = 82
            elif same_region and similarity >= 0.86:
                score = 78
            elif similarity >= 0.92:
                score = 74
            elif same_region and similarity >= 0.74 and tag_overlap:
                score = 70
            if score > int(best["score"]):
                best = {"route_id": route_id, "route_name": row["name"], "score": score}
        return best


def _knowledge_score(intent: RouteRecommendationIntent, candidate: RawRouteCandidate) -> int:
    score = candidate.editorial_rank * 8
    region = _normalize_region(intent.destination_region)
    if region and region in candidate.region:
        score += 40
    text = " ".join([candidate.name, candidate.region, *candidate.aliases, *candidate.scenery])
    score += sum(12 for item in intent.scenery_preferences if item and item in text)
    if candidate.official_status and candidate.official_status != "unverified":
        score += 12
    return score


def _hard_conflict(intent: RouteRecommendationIntent, candidate: RawRouteCandidate) -> bool:
    if candidate.risk_level == "blocked":
        return True
    if intent.fitness_level == "beginner" and candidate.difficulty == "进阶":
        return True
    if intent.max_distance_km is not None and candidate.distance_km is not None and candidate.distance_km > intent.max_distance_km:
        return True
    if intent.min_distance_km is not None and candidate.distance_km is not None and candidate.distance_km < intent.min_distance_km:
        return True
    if intent.max_duration_hours is not None and candidate.duration_hours is not None and candidate.duration_hours > intent.max_duration_hours:
        return True
    if intent.max_ascent_m is not None and candidate.ascent_m is not None and candidate.ascent_m > intent.max_ascent_m:
        return True
    return False


def _normalize_region(value: str | None) -> str:
    return re.sub(r"(省|市|自治区|壮族自治区|回族自治区|维吾尔自治区)$", "", (value or "").strip())


def _fts_term(value: str) -> str:
    return re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]', '', value)


def _normalized_name(value: str) -> str:
    value = re.sub(r"(徒步路线|徒步线路|登山路线|步道|路线|线路)$", "", value.strip())
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.lower())


def _popularity_label(editorial_rank: int, official_status: str) -> str:
    if official_status != "unverified":
        return "官方收录"
    return "省内重点精选" if editorial_rank >= 3 else "省内精选"


def _format_region(province: str, city: str) -> str:
    if province in {"北京", "上海", "天津", "重庆"}:
        return f"{province}市"
    if city in {"甘孜", "阿坝", "迪庆"}:
        return f"{province}省{city}州"
    city_label = city if city.endswith(("市", "地区", "盟")) else f"{city}市"
    return f"{province}省{city_label}"


def _route_id(payload: RouteKnowledgeCreate) -> str:
    if payload.id:
        return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "-", payload.id.strip())[:80]
    base = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]", "-", f"{payload.province}-{payload.city}-{payload.name}")
    base = re.sub(r"-+", "-", base).strip("-")[:48] or "route"
    return f"custom-{base}-{uuid4().hex[:8]}"


def _candidate_region_parts(candidate: RouteImportExtractedCandidate | RouteImportCandidateRecord) -> tuple[str | None, str | None]:
    if candidate.province or candidate.city:
        return candidate.province, candidate.city
    region = (candidate.region or "").strip()
    if not region:
        return None, None
    compact = region.replace("省", "省 ").replace("市", "市 ").replace("自治区", "自治区 ")
    parts = [part.strip() for part in re.split(r"\s+|/|，|,", compact) if part.strip()]
    if len(parts) >= 2:
        province = re.sub(r"(省|市|自治区)$", "", parts[0])
        city = re.sub(r"(市|州|地区)$", "", parts[1])
        return province or parts[0], city or parts[1]
    return region[:20], region[:20]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    province TEXT NOT NULL,
    city TEXT NOT NULL,
    summary TEXT,
    difficulty TEXT,
    distance_km REAL,
    duration_hours REAL,
    ascent_m REAL,
    camping INTEGER,
    seasons TEXT NOT NULL DEFAULT '[]',
    transport_notes TEXT NOT NULL DEFAULT '[]',
    editorial_rank INTEGER NOT NULL DEFAULT 0,
    official_status TEXT NOT NULL DEFAULT 'unverified',
    risk_level TEXT NOT NULL DEFAULT 'normal',
    risk_notes TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    last_verified_at TEXT
);
CREATE TABLE IF NOT EXISTS route_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    UNIQUE(route_id, alias)
);
CREATE TABLE IF NOT EXISTS route_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    published_at TEXT,
    summary TEXT,
    UNIQUE(route_id, url)
);
CREATE TABLE IF NOT EXISTS route_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    UNIQUE(route_id, tag)
);
CREATE TABLE IF NOT EXISTS route_import_jobs (
    id TEXT PRIMARY KEY,
    source_url TEXT,
    raw_text TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TEXT NOT NULL,
    warnings TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS route_import_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES route_import_jobs(id) ON DELETE CASCADE,
    extracted_name TEXT NOT NULL,
    region TEXT,
    province TEXT,
    city TEXT,
    summary TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    distance_km REAL,
    duration_hours REAL,
    ascent_m REAL,
    risk_notes TEXT NOT NULL DEFAULT '[]',
    source_title TEXT,
    source_url TEXT,
    source_type TEXT NOT NULL DEFAULT 'imported-document',
    matched_route_id TEXT,
    matched_route_name TEXT,
    match_score INTEGER NOT NULL DEFAULT 0,
    suggested_action TEXT NOT NULL DEFAULT 'create',
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_routes_region ON routes(province, city);
CREATE INDEX IF NOT EXISTS idx_routes_status_rank ON routes(status, editorial_rank DESC);
CREATE INDEX IF NOT EXISTS idx_route_import_candidates_job ON route_import_candidates(job_id);
"""

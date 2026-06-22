from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from threading import Lock
from typing import Protocol

from app.config import PROJECT_ROOT
from app.models import RecommendationEvidence, RouteRecommendationIntent
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

    def _seed(self, connection: sqlite3.Connection) -> None:
        for record in starter_routes():
            connection.execute(
                """
                INSERT INTO routes (
                    id, name, province, city, difficulty, distance_km, duration_hours, ascent_m,
                    camping, seasons, editorial_rank, official_status, risk_level, risk_notes, status, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"], record["name"], record["province"], record["city"], record.get("difficulty"),
                    record.get("distance_km"), record.get("duration_hours"), record.get("ascent_m"),
                    record.get("camping"), json.dumps(record["seasons"], ensure_ascii=False),
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
                    "INSERT INTO route_sources (route_id, title, url, source_type) VALUES (?, ?, ?, ?)",
                    (record["id"], source["title"], source["url"], source["source_type"]),
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
            RecommendationEvidence(title=item[0], url=item[1], source_type=item[2])
            for item in connection.execute(
                "SELECT title, url, source_type FROM route_sources WHERE route_id = ? ORDER BY source_type = 'official' DESC, id",
                (route_id,),
            )
        ]
        risk_notes = json.loads(row["risk_notes"] or "[]")
        seasons = json.loads(row["seasons"] or "[]")
        return RawRouteCandidate(
            name=row["name"], aliases=aliases, region=_format_region(row["province"], row["city"]),
            difficulty=row["difficulty"], distance_km=row["distance_km"], duration_hours=row["duration_hours"],
            ascent_m=row["ascent_m"], scenery=tags, seasons=seasons,
            camping=None if row["camping"] is None else bool(row["camping"]),
            evidence=sources, verification_items=risk_notes, retrieval_source="knowledge_base",
            popularity_label=_popularity_label(row["editorial_rank"], row["official_status"]),
            last_verified_at=row["last_verified_at"], official_status=row["official_status"],
            editorial_rank=row["editorial_rank"], risk_level=row["risk_level"],
        )


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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    province TEXT NOT NULL,
    city TEXT NOT NULL,
    difficulty TEXT,
    distance_km REAL,
    duration_hours REAL,
    ascent_m REAL,
    camping INTEGER,
    seasons TEXT NOT NULL DEFAULT '[]',
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
    UNIQUE(route_id, url)
);
CREATE TABLE IF NOT EXISTS route_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    UNIQUE(route_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_routes_region ON routes(province, city);
CREATE INDEX IF NOT EXISTS idx_routes_status_rank ON routes(status, editorial_rank DESC);
"""

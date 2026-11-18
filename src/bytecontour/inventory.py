"""Bounded browsing and storage summaries for published scan snapshots."""

from dataclasses import dataclass
from pathlib import Path
from shutil import disk_usage
from typing import Literal, Self

from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from bytecontour.database import InventoryEntry as Row
from bytecontour.database import Scan, ScanRepository, ScanSummary

PAGE_SIZE = 200
CHART_LIMIT = 12
TREE_LIMIT = 100
type Sort = Literal["path", "size", "modified", "extension"]
type Cursor = tuple[str | int, str]


@dataclass(frozen=True, slots=True)
class InventoryQuery:
    scan_id: int | None = None
    folder: str = ""
    search: str = ""
    extension: str | None = None
    minimum: int | None = None
    maximum: int | None = None
    modified_from: int | None = None
    modified_before: int | None = None
    files_only: bool = False
    exact_path: str | None = None
    sort: Sort = "path"
    descending: bool = False
    after: Cursor | None = None
    overview: bool = True
    dashboard: bool = False


@dataclass(frozen=True, slots=True)
class Item:
    path: str
    kind: str
    size: int
    allocated: int | None
    modified: int
    extension: str
    file_count: int
    incomplete: bool

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    def cursor(self, sort: Sort) -> Cursor:
        keys: dict[Sort, str | int] = {
            "path": self.path,
            "size": self.size,
            "modified": self.modified,
            "extension": self.extension,
        }
        return keys[sort], self.path

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            row.path,
            row.kind,
            row.logical_bytes,
            row.allocated_bytes,
            row.modified_ns,
            row.extension,
            row.file_count,
            row.incomplete,
        )


@dataclass(frozen=True, slots=True)
class Usage:
    extension: str
    size: int
    files: int


@dataclass(frozen=True, slots=True)
class Overview:
    folder: Item
    folders: tuple[Item, ...]
    more_folders: bool
    tiles: tuple[Item, ...]
    largest_files: tuple[Item, ...]
    largest_folders: tuple[Item, ...]
    types: tuple[Usage, ...]
    capacity: tuple[int, int, int] | None


@dataclass(frozen=True, slots=True)
class InventoryResult:
    query: InventoryQuery
    scan: ScanSummary | None
    entries: tuple[Item, ...] = ()
    next_page: Cursor | None = None
    overview: Overview | None = None


def _descendants(folder: str) -> tuple[ColumnElement[bool], ...]:
    return () if not folder else (Row.path >= folder + "/", Row.path < folder + "0")


class InventoryReader:
    def __init__(self, repository: ScanRepository) -> None:
        self._repository = repository

    def read(self, query: InventoryQuery) -> InventoryResult:
        with Session(self._repository.engine) as session:
            scans = select(Scan).where(Scan.state.in_(("completed", "partial")))
            if query.scan_id is not None:
                scans = scans.where(Scan.id == query.scan_id)
            scan = session.scalar(scans.order_by(Scan.id.desc()).limit(1))
            if scan is None:
                return InventoryResult(query, None)
            summary = ScanSummary.from_scan(scan)
            entries, next_page = (
                ((), None) if query.dashboard else self._page(session, scan.id, query)
            )
            overview = self._overview(session, summary, query.folder) if query.overview else None
            return InventoryResult(query, summary, entries, next_page, overview)

    @staticmethod
    def _page(
        session: Session, scan_id: int, query: InventoryQuery
    ) -> tuple[tuple[Item, ...], Cursor | None]:
        statement = select(Row).where(Row.scan_id == scan_id)
        file_filter = query.files_only or any(
            value is not None
            for value in (
                query.extension,
                query.minimum,
                query.maximum,
                query.modified_from,
                query.modified_before,
            )
        )
        if query.exact_path is not None:
            statement = statement.where(Row.path == query.exact_path, Row.kind == "file")
        elif query.search or file_filter:
            statement = statement.where(Row.parent.is_not(None), *_descendants(query.folder))
        else:
            statement = statement.where(Row.parent == query.folder)
        if file_filter:
            statement = statement.where(Row.kind == "file")
        if query.search:
            statement = statement.where(Row.path.icontains(query.search, autoescape=True))
        if query.extension is not None:
            statement = statement.where(Row.extension == query.extension)
        if query.minimum is not None:
            statement = statement.where(Row.logical_bytes >= query.minimum)
        if query.maximum is not None:
            statement = statement.where(Row.logical_bytes <= query.maximum)
        if query.modified_from is not None:
            statement = statement.where(Row.modified_ns >= query.modified_from)
        if query.modified_before is not None:
            statement = statement.where(Row.modified_ns < query.modified_before)
        column = {
            "path": Row.path,
            "size": Row.logical_bytes,
            "modified": Row.modified_ns,
            "extension": Row.extension,
        }[query.sort]
        if query.after is not None:
            key, path = query.after
            if query.sort == "path":
                statement = statement.where(
                    Row.path < path if query.descending else Row.path > path
                )
            else:
                order_key = tuple_(column, Row.path)
                boundary = tuple_(literal(key), literal(path))
                statement = statement.where(
                    order_key < boundary if query.descending else order_key > boundary
                )
        columns = (column,) if query.sort == "path" else (column, Row.path)
        ordered = [col.desc() if query.descending else col.asc() for col in columns]
        rows = [
            Item.from_row(row)
            for row in session.scalars(statement.order_by(*ordered).limit(PAGE_SIZE + 1))
        ]
        more = rows[PAGE_SIZE - 1].cursor(query.sort) if len(rows) > PAGE_SIZE else None
        return tuple(rows[:PAGE_SIZE]), more

    @staticmethod
    def _overview(session: Session, scan: ScanSummary, folder: str) -> Overview:
        current = session.get(Row, (scan.id, folder))
        if current is None or current.kind != "directory":
            raise ValueError("This folder is missing from the saved inventory.")
        children = select(Row).where(Row.scan_id == scan.id, Row.parent == folder)
        folders = list(
            session.scalars(
                children.where(Row.kind == "directory").order_by(Row.path).limit(TREE_LIMIT + 1)
            )
        )
        largest_folders = session.scalars(
            children.where(Row.kind == "directory")
            .order_by(Row.logical_bytes.desc(), Row.path)
            .limit(CHART_LIMIT)
        )
        tiles = session.scalars(
            children.order_by(Row.logical_bytes.desc(), Row.path).limit(CHART_LIMIT)
        )
        files = select(Row).where(Row.scan_id == scan.id, Row.kind == "file", *_descendants(folder))
        largest = session.scalars(
            files.order_by(Row.logical_bytes.desc(), Row.path).limit(CHART_LIMIT)
        )
        size = func.sum(Row.logical_bytes)
        types = session.execute(
            select(Row.extension, size, func.count())
            .where(Row.scan_id == scan.id, Row.kind == "file", *_descendants(folder))
            .group_by(Row.extension)
            .order_by(size.desc(), Row.extension)
            .limit(CHART_LIMIT)
        )
        try:
            total, used, free = disk_usage(Path(scan.root))
            capacity = (total, used, free)
        except OSError:
            capacity = None
        return Overview(
            Item.from_row(current),
            tuple(map(Item.from_row, folders[:TREE_LIMIT])),
            len(folders) > TREE_LIMIT,
            tuple(map(Item.from_row, tiles)),
            tuple(map(Item.from_row, largest)),
            tuple(map(Item.from_row, largest_folders)),
            tuple(Usage(ext, count_bytes, count) for ext, count_bytes, count in types),
            capacity,
        )

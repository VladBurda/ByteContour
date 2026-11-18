"""Explainable, read-only storage recommendations from local evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import time
from typing import Literal, Protocol

from sqlalchemy import and_, delete, func, or_, select, true, tuple_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from bytecontour.database import (
    InventoryEntry as Entry,
)
from bytecontour.database import (
    RecommendationDismissal,
    Scan,
    ScanRepository,
    StorageSnapshot,
    WatchRoot,
    _utc_now,
)
from bytecontour.duplicate_store import DuplicateStore

DAY = 86400
MIB = 1024**2
_LIMIT = 12
type Risk = Literal["Low", "Medium", "High"]


@dataclass(frozen=True, slots=True)
class Thresholds:
    old_days: int = 180
    large_mib: int = 512
    growth_mib: int = 256
    growth_days: int = 7
    duplicate_mib: int = 64
    installer_count: int = 2
    temp_days: int = 30
    temp_count: int = 10

    def validate(self) -> None:
        if not (
            1 <= self.old_days <= 36500
            and 1 <= self.temp_days <= 36500
            and 1 <= self.growth_days <= 365
            and all(
                1 <= size <= 2**30 for size in (self.large_mib, self.growth_mib, self.duplicate_mib)
            )
            and all(1 <= count <= 1_000_000 for count in (self.installer_count, self.temp_count))
        ):
            raise ValueError("Recommendation thresholds are outside the supported range.")


@dataclass(frozen=True, slots=True)
class Recommendation:
    rule: str
    scope: str
    title: str
    reason: str
    risk: Risk
    next_step: str
    evidence: tuple[str | int | float | None, ...]
    logical_potential: int | None = None
    allocated_potential: int | None = None

    @property
    def identity(self) -> tuple[str, str]:
        key = json.dumps((self.rule, self.scope))
        data = json.dumps(self.evidence, separators=(",", ":")).encode()
        return key, hashlib.sha256(data).hexdigest()


class Rule(Protocol):
    def evaluate(self, context: Context, thresholds: Thresholds) -> Iterable[Recommendation]: ...


@dataclass(frozen=True, slots=True)
class Context:
    session: Session
    scan: Scan | None
    duplicates: DuplicateStore
    now: float


def _size(value: int) -> str:
    return f"{value / MIB:,.1f} MiB" if value >= MIB else f"{value:,} bytes"


def _large(ctx: Context, config: Thresholds) -> Iterable[Recommendation]:
    if ctx.scan is None:
        return
    cutoff = int(ctx.now - config.old_days * DAY) * 10**9
    query = (
        select(Entry)
        .where(
            Entry.scan_id == ctx.scan.id,
            Entry.kind == "file",
            Entry.logical_bytes >= config.large_mib * MIB,
            Entry.modified_ns <= cutoff,
        )
        .order_by(Entry.logical_bytes.desc(), Entry.path)
        .limit(_LIMIT)
    )
    for row in ctx.session.scalars(query):
        yield Recommendation(
            "old-large",
            f"{ctx.scan.root}/{row.path}",
            "Large file with an old modification date",
            f"This file occupies {_size(row.logical_bytes)} in the inventory and has not been "
            f"modified for at least {config.old_days} days.",
            "High",
            "Review its purpose and backups in Disk Explorer.",
            (row.identity, row.logical_bytes, row.modified_ns, row.change_ns),
        )


def _clusters(ctx: Context, config: Thresholds, *, installers: bool) -> Iterable[Recommendation]:
    if ctx.scan is None:
        return
    if installers:
        # Component matching avoids classifying folders such as DownloadsArchive as Downloads.
        downloads = (
            true()
            if ctx.scan.root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
            == "downloads"
            else or_(
                func.lower(Entry.parent) == "downloads",
                func.lower(Entry.parent).like("downloads/%"),
                func.lower(Entry.parent).like("%/downloads"),
                func.lower(Entry.parent).like("%/downloads/%"),
            )
        )
        filename = func.lower(Entry.path)
        packages = or_(
            Entry.extension.in_((".msi", ".msix", ".msp", ".dmg", ".pkg", ".appx")),
            and_(
                Entry.extension == ".exe",
                or_(filename.like("%setup.exe"), filename.like("%installer.exe")),
                filename.not_like("%uninstaller.exe"),
            ),
        )
        conditions = (packages, downloads)
        count = config.installer_count
    else:
        conditions = (
            Entry.extension.in_((".tmp", ".temp")),
            Entry.modified_ns <= int(ctx.now - config.temp_days * DAY) * 10**9,
        )
        count = config.temp_count
    total = func.sum(Entry.logical_bytes)
    query = (
        select(
            Entry.parent,
            func.count(),
            total,
            func.max(Entry.modified_ns),
            func.max(Entry.change_ns),
        )
        .where(
            Entry.scan_id == ctx.scan.id,
            Entry.kind == "file",
            *conditions,
        )
        .group_by(Entry.parent)
        .having(func.count() >= count)
        .order_by(
            total.desc(),
            Entry.parent,
        )
        .limit(_LIMIT)
    )
    for folder, files, size, modified, changed in ctx.session.execute(query):
        scope = f"{ctx.scan.root}/{folder}" if folder else ctx.scan.root
        if installers:
            yield Recommendation(
                "installers",
                scope,
                "Installer packages in Downloads",
                f"{files:,} installer packages occupy {_size(size)} in this folder. "
                "Some may already have been used.",
                "Medium",
                "Check whether you still need each installer or can download it again.",
                (files, size, modified, changed),
            )
        else:
            yield Recommendation(
                "temporary",
                scope,
                "Concentration of old temporary files",
                f"{files:,} temporary files occupy {_size(size)}. Their modification dates "
                f"are at least {config.temp_days} days old.",
                "Medium",
                "Check whether the owning application still needs these files.",
                (files, size, modified, changed),
            )


def _installers(ctx: Context, config: Thresholds) -> Iterable[Recommendation]:
    return _clusters(ctx, config, installers=True)


def _temporary(ctx: Context, config: Thresholds) -> Iterable[Recommendation]:
    return _clusters(ctx, config, installers=False)


def _duplicates(ctx: Context, config: Thresholds) -> Iterable[Recommendation]:
    if ctx.scan is None:
        return
    report = ctx.duplicates.report(ctx.scan.id)
    if report.run_id is not None and report.logical >= config.duplicate_mib * MIB:
        yield Recommendation(
            "duplicates",
            ctx.scan.root,
            "Duplicate copies worth reviewing",
            f"The saved duplicate run found {report.group_count:,} groups with "
            f"{_size(report.logical)} of potential logical savings after keeper and link checks.",
            "Medium",
            "Review the groups in Duplicate Finder and verify candidates against keepers.",
            (ctx.scan.id, report.run_id, report.group_count, report.logical, report.allocated),
            report.logical,
            report.allocated,
        )


def _growth(ctx: Context, config: Thresholds) -> Iterable[Recommendation]:
    for watch in ctx.session.scalars(select(WatchRoot).order_by(WatchRoot.id)):
        samples = select(StorageSnapshot).where(
            StorageSnapshot.root_id == watch.id,
            StorageSnapshot.error.is_(None),
            StorageSnapshot.observed >= ctx.now - config.growth_days * DAY,
            StorageSnapshot.observed <= ctx.now,
        )
        start = ctx.session.scalar(
            samples.order_by(StorageSnapshot.observed, StorageSnapshot.id).limit(1)
        )
        end = ctx.session.scalar(
            samples.order_by(StorageSnapshot.observed.desc(), StorageSnapshot.id.desc()).limit(1)
        )
        if start is None or end is None:
            continue
        growth = end.logical - start.logical
        if growth >= config.growth_mib * MIB:
            hours = max(0, end.observed - start.observed) / 3600
            elapsed = f"{hours:.1f} hours" if hours >= 0.1 else "less than 6 minutes"
            yield Recommendation(
                "growth",
                watch.path,
                "Watched folder grew",
                f"Complete samples show an increase of {_size(growth)} over {elapsed}, "
                f"within the last {config.growth_days} days.",
                "Low",
                "Review recent events and the growth chart in Storage Monitor.",
                (start.logical, end.logical, start.observed),
            )


@dataclass(frozen=True, slots=True)
class _FunctionRule:
    evaluate: Callable[[Context, Thresholds], Iterable[Recommendation]]


DEFAULT_RULES: tuple[Rule, ...] = tuple(
    _FunctionRule(rule) for rule in (_growth, _duplicates, _large, _installers, _temporary)
)


@dataclass(frozen=True, slots=True)
class RecommendationPage:
    rows: tuple[Recommendation, ...]
    dismissed: frozenset[tuple[str, str]]
    scan_scope: str | None
    partial: bool


class RecommendationService:
    def __init__(self, repo: ScanRepository, rules: tuple[Rule, ...] = DEFAULT_RULES) -> None:
        self._engine = repo.engine
        self._duplicates = DuplicateStore(repo)
        self._rules = rules

    def read(self, config: Thresholds, *, now: float | None = None) -> RecommendationPage:
        config.validate()
        with Session(self._engine) as session:
            scan = session.scalar(
                select(Scan)
                .where(Scan.state.in_(("completed", "partial")))
                .order_by(Scan.id.desc())
                .limit(1)
            )
            context = Context(session, scan, self._duplicates, time() if now is None else now)
            rows = tuple(item for rule in self._rules for item in rule.evaluate(context, config))
            dismissal = RecommendationDismissal
            dismissed = (
                frozenset(
                    (key, stamp)
                    for key, stamp in session.execute(
                        select(dismissal.key, dismissal.fingerprint).where(
                            tuple_(dismissal.key, dismissal.fingerprint).in_(
                                [item.identity for item in rows]
                            )
                        )
                    )
                )
                if rows
                else frozenset()
            )
            return RecommendationPage(
                rows, dismissed, scan.root if scan else None, bool(scan and scan.state == "partial")
            )

    def set_dismissed(self, item: Recommendation, dismissed: bool) -> None:
        key, fingerprint = item.identity
        table = RecommendationDismissal
        with Session(self._engine) as session, session.begin():
            if dismissed:
                session.execute(
                    insert(table)
                    .values(key=key, fingerprint=fingerprint, dismissed_at=_utc_now())
                    .on_conflict_do_nothing()
                )
            else:
                session.execute(
                    delete(table).where(table.key == key, table.fingerprint == fingerprint)
                )

"""Coordinate scan lifecycle and publish consistent inventory snapshots."""

import logging
import os
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from disksense.database import ScanRepository, ScanSummary
from disksense.scanner import Entry, Inventory, ScanCancelled, ScanControl, ScanProgress, ScanState

_logger = logging.getLogger(__name__)


class ScanService:
    def __init__(self, repository: ScanRepository) -> None:
        self.repository = repository

    def scan(
        self,
        root: Path,
        incremental: bool,
        control: ScanControl,
        report: Callable[[ScanProgress], None],
    ) -> ScanSummary:
        root = Path(os.path.abspath(root))
        scan_id, _ = self.repository.begin(str(root), os.path.normcase(str(root)), incremental)
        inventory = Inventory(root)

        def save(batch: list[Entry]) -> None:
            control.checkpoint()
            self.repository.append(scan_id, batch)

        state: ScanState
        try:
            inventory.run(control, save, report)
            control.checkpoint(seal=True)
            state = "partial" if inventory.errors else "completed"
            return self.repository.finish(
                scan_id, state, inventory.progress(), inventory.first_error
            )
        except ScanCancelled:
            return self.repository.finish(scan_id, "cancelled", inventory.progress(), None)
        except OSError as error:
            return self.repository.finish(scan_id, "failed", inventory.progress(), str(error))
        except SQLAlchemyError as error:
            _logger.exception("scan_storage_error", extra={"context": {"scan_id": scan_id}})
            try:
                return self.repository.finish(
                    scan_id, "failed", inventory.progress(), "SQLite could not save the scan."
                )
            except SQLAlchemyError:
                # Keep running state for recovery if the database remains unavailable.
                raise error from None

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import Database

logger = logging.getLogger(__name__)

_BACKUP_RE = re.compile(r"^(auto|manual|pre-restore)-\d{8}-\d{6}(?:-[0-9a-f]{6})?\.sqlite3$")


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: Path
    size: int
    mtime: float
    kind: str


class BackupManager:
    def __init__(self, db: Database, backup_dir: str, timezone: ZoneInfo) -> None:
        self.db = db
        self.backup_dir = Path(backup_dir)
        self.timezone = timezone
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._operation_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="sqlite-backup-loop")

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _safe_path(self, name: str) -> Path:
        if not _BACKUP_RE.fullmatch(name):
            raise ValueError("Некорректное имя резервной копии")
        path = (self.backup_dir / name).resolve()
        root = self.backup_dir.resolve()
        if path.parent != root:
            raise ValueError("Некорректный путь резервной копии")
        return path

    async def list_backups(self) -> list[BackupInfo]:
        def scan() -> list[BackupInfo]:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            result: list[BackupInfo] = []
            for path in self.backup_dir.glob("*.sqlite3"):
                if not _BACKUP_RE.fullmatch(path.name):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if path.name.startswith("pre-restore-"):
                    kind = "pre-restore"
                elif path.name.startswith("auto-"):
                    kind = "auto"
                else:
                    kind = "manual"
                result.append(BackupInfo(path.name, path, stat.st_size, stat.st_mtime, kind))
            result.sort(key=lambda item: item.mtime, reverse=True)
            return result

        return await asyncio.to_thread(scan)

    async def create_backup(self, kind: str = "manual") -> BackupInfo:
        if kind not in {"auto", "manual", "pre-restore"}:
            raise ValueError("Unsupported backup kind")
        async with self._operation_lock:
            now = datetime.now(self.timezone)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            base = f"{kind}-{now:%Y%m%d-%H%M%S}"
            name = base + ".sqlite3"
            path = self.backup_dir / name
            if path.exists():
                import secrets

                name = f"{base}-{secrets.token_hex(3)}.sqlite3"
                path = self.backup_dir / name
            temp = path.with_suffix(".tmp")
            await asyncio.to_thread(self._backup_sync, Path(self.db.path), temp)
            os.replace(temp, path)
            await self.prune()
            stat = path.stat()
            logger.info("SQLite backup created: %s (%s bytes)", path, stat.st_size)
            return BackupInfo(path.name, path, stat.st_size, stat.st_mtime, kind)

    @staticmethod
    def _backup_sync(source_path: Path, destination_path: Path) -> None:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            destination_path.unlink()
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(destination_path, timeout=30)
        try:
            source.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise RuntimeError(f"SQLite integrity_check failed: {result}")
            destination.commit()
        finally:
            destination.close()
            source.close()

    async def prune(self) -> None:
        try:
            retention = int(await self.db.get_setting("backup_retention", "30") or 30)
        except ValueError:
            retention = 30
        retention = max(3, min(retention, 365))
        backups = await self.list_backups()
        for item in backups[retention:]:
            try:
                await asyncio.to_thread(item.path.unlink)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Could not prune backup %s", item.path, exc_info=True)

    async def delete_backup(self, name: str) -> None:
        path = self._safe_path(name)
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            return

    async def restore_backup(self, name: str) -> str:
        path = self._safe_path(name)
        if not path.exists():
            raise FileNotFoundError(name)
        async with self._operation_lock:
            # Always make a safety point before replacing the live DB.
            now = datetime.now(self.timezone)
            safety_name = f"pre-restore-{now:%Y%m%d-%H%M%S}.sqlite3"
            safety_path = self.backup_dir / safety_name
            if safety_path.exists():
                import secrets

                safety_name = f"pre-restore-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}.sqlite3"
                safety_path = self.backup_dir / safety_name
            await asyncio.to_thread(self._backup_sync, Path(self.db.path), safety_path)
            await asyncio.to_thread(self._validate_sync, path)
            async with self.db.exclusive_maintenance():
                await asyncio.to_thread(self._restore_sync, path, Path(self.db.path))
            # Re-run current schema migrations in case an older compatible backup was selected.
            await self.db.init()
            await self.prune()
            logger.warning("SQLite database restored from %s; safety backup: %s", path, safety_path)
            return safety_name

    @staticmethod
    def _validate_sync(path: Path) -> None:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise RuntimeError(f"SQLite integrity_check failed: {result}")
            # A project backup must at least contain settings and submissions.
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if not {"settings", "form_submissions"}.issubset(tables):
                raise RuntimeError("Файл не похож на базу Telegram AutoReply")
        finally:
            conn.close()

    @staticmethod
    def _restore_sync(source_path: Path, target_path: Path) -> None:
        source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
        target = sqlite3.connect(target_path, timeout=30)
        try:
            try:
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass
            source.backup(target)
            target.commit()
            result = target.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise RuntimeError(f"Restored database integrity_check failed: {result}")
        finally:
            target.close()
            source.close()

    async def _has_auto_backup_for_today(self) -> bool:
        prefix = f"auto-{datetime.now(self.timezone):%Y%m%d}-"
        return any(item.name.startswith(prefix) for item in await self.list_backups())

    async def _loop(self) -> None:
        # Check often enough to react to a changed backup hour without restart.
        while not self._stop.is_set():
            try:
                enabled = (await self.db.get_setting("backups_enabled", "1")) == "1"
                try:
                    hour = int(await self.db.get_setting("backup_hour_local", "4") or 4)
                except ValueError:
                    hour = 4
                hour = max(0, min(hour, 23))
                now = datetime.now(self.timezone)
                if enabled and now.hour >= hour and not await self._has_auto_backup_for_today():
                    await self.create_backup("auto")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Automatic SQLite backup failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=300)
            except asyncio.TimeoutError:
                pass

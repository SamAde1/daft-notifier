from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


_LOG_FILE_RE = re.compile(r"^daft_notifier_(dev|prod)_(\d{4}-\d{2}-\d{2})(?:_(\d+))?\.log$")
_LOG_ID_RE = re.compile(r"\[(\d+)\]")
_VALID_ENVIRONMENTS = {"dev", "prod"}
_VALID_LEVELS = {"debug", "info", "error"}


def parse_environment(value: str) -> str:
    parsed = value.strip().lower()
    if parsed not in _VALID_ENVIRONMENTS:
        raise ValueError(f"Invalid environment '{value}'. Expected one of: dev, prod.")
    return parsed


def parse_log_level(value: str) -> str:
    parsed = value.strip().lower()
    if parsed not in _VALID_LEVELS:
        raise ValueError(f"Invalid log level '{value}'. Expected one of: debug, info, error.")
    return parsed


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value '{value}'.")


def logging_level_from_name(name: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "error": logging.ERROR,
    }[parse_log_level(name)]


_ENV_LOG_DEFAULTS: dict[str, dict[str, int]] = {
    "dev":  {"max_entries_per_file": 1000, "max_log_files": 5},
    "prod": {"max_entries_per_file": 1500, "max_log_files": 10},
}


@dataclass(slots=True)
class LoggingRuntimeConfig:
    environment: str = "dev"
    log_level: str = "info"
    write_logs: bool = True
    log_dir: str = "./logs"
    max_entries_per_file: int = 0  # 0 = use environment default
    max_log_files: int = 0         # 0 = use environment default

    def __post_init__(self) -> None:
        defaults = _ENV_LOG_DEFAULTS.get(self.environment, _ENV_LOG_DEFAULTS["dev"])
        if self.max_entries_per_file <= 0:
            self.max_entries_per_file = defaults["max_entries_per_file"]
        if self.max_log_files <= 0:
            self.max_log_files = defaults["max_log_files"]


class _RecordContextFilter(logging.Filter):
    def __init__(self, environment: str):
        super().__init__()
        self._environment = environment

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "environment"):
            record.environment = self._environment
        if not hasattr(record, "log_id"):
            record.log_id = "-"
        return True


class _SingleLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return rendered.replace("\r", "\\r").replace("\n", "\\n")


class IncrementalLogFileHandler(logging.Handler):
    def __init__(
        self,
        *,
        log_dir: str,
        environment: str,
        max_entries_per_file: int = 1000,
        max_log_files: int = 5,
    ):
        super().__init__()
        self._log_dir = Path(log_dir)
        self._environment = parse_environment(environment)
        self._max_entries_per_file = max_entries_per_file
        self._max_log_files = max_log_files

        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._current_file, self._current_count, self._next_log_id = self._bootstrap_state()
        self._enforce_file_limit()

    def _list_log_files(self) -> list[Path]:
        files: list[Path] = []
        for path in self._log_dir.glob(f"daft_notifier_{self._environment}_*.log"):
            match = _LOG_FILE_RE.match(path.name)
            if match and match.group(1) == self._environment:
                files.append(path)
        files.sort(key=lambda p: (p.stat().st_mtime, p.name))
        return files

    def _bootstrap_state(self) -> tuple[Path, int, int]:
        files = self._list_log_files()
        if not files:
            first_file = self._create_new_file()
            return first_file, 0, 1

        latest = files[-1]
        first_id, last_id, entry_count = self._inspect_file(latest)
        # first_id is intentionally read for integrity checks and future diagnostics.
        _ = first_id
        next_id = last_id + 1 if last_id > 0 else 1
        if entry_count < self._max_entries_per_file:
            return latest, entry_count, next_id

        next_file = self._create_new_file()
        return next_file, 0, next_id

    def _inspect_file(self, path: Path) -> tuple[int, int, int]:
        first_id = 0
        last_id = 0
        entry_count = 0
        if not path.exists():
            return first_id, last_id, entry_count

        first_line: str | None = None
        last_line: str | None = None
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw_line.strip():
                continue
            entry_count += 1
            if first_line is None:
                first_line = raw_line
            last_line = raw_line

        if first_line:
            first_id = self._parse_log_id(first_line)
        if last_line:
            last_id = self._parse_log_id(last_line)
        return first_id, last_id, entry_count

    @staticmethod
    def _parse_log_id(line: str) -> int:
        match = _LOG_ID_RE.search(line)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    def _create_new_file(self) -> Path:
        today = datetime.now().strftime("%Y-%m-%d")
        base_name = f"daft_notifier_{self._environment}_{today}.log"
        base_path = self._log_dir / base_name
        if not base_path.exists():
            base_path.touch()
            return base_path

        suffix = 2
        while True:
            candidate = self._log_dir / f"daft_notifier_{self._environment}_{today}_{suffix}.log"
            if not candidate.exists():
                candidate.touch()
                return candidate
            suffix += 1

    def _enforce_file_limit(self) -> None:
        files = self._list_log_files()
        while len(files) > self._max_log_files:
            files[0].unlink(missing_ok=True)
            files = self._list_log_files()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._current_count >= self._max_entries_per_file:
                self._current_file = self._create_new_file()
                self._current_count = 0
                self._enforce_file_limit()

            line = self._format_file_line(record, self._next_log_id)
            with self._current_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._current_count += 1
            self._next_log_id += 1
        except Exception:
            self.handleError(record)

    def _format_file_line(self, record: logging.LogRecord, log_id: int) -> str:
        cloned = logging.makeLogRecord(record.__dict__.copy())
        cloned.environment = self._environment
        cloned.log_id = f"{log_id:07d}"
        formatter = self.formatter
        if formatter is None:
            formatter = logging.Formatter("%(message)s")
        return formatter.format(cloned)


def setup_logging(config: LoggingRuntimeConfig) -> None:
    environment = parse_environment(config.environment)
    level = logging_level_from_name(config.log_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    context_filter = _RecordContextFilter(environment=environment)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.addFilter(context_filter)
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s env=%(environment)s %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root_logger.addHandler(console_handler)

    if config.write_logs:
        file_handler = IncrementalLogFileHandler(
            log_dir=config.log_dir,
            environment=environment,
            max_entries_per_file=config.max_entries_per_file,
            max_log_files=config.max_log_files,
        )
        file_handler.setLevel(level)
        file_handler.addFilter(context_filter)
        file_handler.setFormatter(
            _SingleLineFormatter(
                "[%(log_id)s] %(asctime)s %(levelname)-5s env=%(environment)s %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root_logger.addHandler(file_handler)

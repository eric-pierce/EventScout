"""Editing config.yaml from the app (web UI, CLI) without losing comments, ordering or ${VAR} placeholders.

Every change is validated as a complete Config before anything is written, the previous file is copied to
<data_dir>/config-backups/ first, and the file is rewritten in place (not replaced), so it also works when
config.yaml is a single-file Docker bind mount.
"""

from __future__ import annotations

import hashlib
import io
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

from eventscout.config import Config, Profile, _interpolate

KEEP_BACKUPS = 30


class ConfigFileError(Exception):
    """A change that can't be saved: invalid config, read-only file, or edited elsewhere meanwhile."""


class _NoChange(Exception):
    """Raised by an edit callback to leave the file untouched."""


def _yaml() -> YAML:
    y = YAML()  # round-trip mode: keeps comments, key order, quoting and flow/block styles
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _plain(node: Any) -> Any:
    """ruamel round-trip containers -> plain dicts/lists (for validating a section)."""
    if isinstance(node, dict):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    return node


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def validate_text(text: str) -> Config:
    """Parse and validate config text exactly as load_config would (minus env side effects)."""
    try:
        raw = YAML(typ="safe").load(text) or {}
    except YAMLError as exc:
        raise ConfigFileError(f"YAML syntax error: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigFileError("config.yaml must be a mapping of sections (home:, profile:, sources:, ...)")
    try:
        return Config.model_validate(_interpolate(raw))
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])
        raise ConfigFileError(f"invalid configuration: {problems}") from None


class ConfigFile:
    def __init__(self, path: str | Path, backup_dir: str | Path | None = None):
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- state
    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def writable(self) -> bool:
        return self.exists and os.access(self.path, os.W_OK)

    def read_only_reason(self) -> str | None:
        if not self.exists:
            return f"{self.path} does not exist"
        if not os.access(self.path, os.W_OK):
            return (f"{self.path} is read-only for the app (uid {os.getuid()}). In docker-compose.yml mount it without "
                    f"':ro', and on Linux hosts make the file writable by uid 1000: sudo chown 1000 config.yaml")
        return None

    def read_text(self) -> str:
        return self.path.read_text() if self.exists else ""

    # ---------------------------------------------------------------- writes
    def write_text(self, text: str, *, base_hash: str | None = None) -> Config:
        """Replace the whole file (the raw editor). `base_hash` guards against overwriting someone else's edit."""
        with self._lock:
            if base_hash is not None and base_hash != text_hash(self.read_text()):
                raise ConfigFileError("config.yaml changed on disk since you opened it; reload and re-apply your edits")
            cfg = validate_text(text)
            self._write(text)
            return cfg

    def edit(self, change: Callable[[CommentedMap], None]) -> Config | None:
        """Apply `change` to the round-trip document, validate the result, then write it.
        Returns None (file untouched) when `change` raises _NoChange."""
        with self._lock:
            y = _yaml()
            try:
                doc = y.load(self.read_text()) or CommentedMap()
            except YAMLError as exc:
                raise ConfigFileError(f"config.yaml has a YAML syntax error, fix it first: {exc}") from None
            try:
                change(doc)
            except _NoChange:
                return None
            buf = io.StringIO()
            y.dump(doc, buf)
            text = buf.getvalue()
            cfg = validate_text(text)
            self._write(text)
            return cfg

    def _write(self, text: str) -> None:
        reason = self.read_only_reason()
        if reason:
            raise ConfigFileError(reason)
        old = self.read_text()
        if old == text:
            return
        self._backup(old)
        # In place (truncate + write) rather than write-and-rename: renaming over a single-file bind mount fails.
        with open(self.path, "r+", encoding="utf-8") as fh:
            fh.seek(0)
            fh.write(text)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())

    def _backup(self, text: str) -> None:
        if not self.backup_dir or not text:
            return
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            (self.backup_dir / f"config-{stamp}.yaml").write_text(text)
            for old in sorted(self.backup_dir.glob("config-*.yaml"))[:-KEEP_BACKUPS]:
                old.unlink(missing_ok=True)
        except OSError:
            pass  # a failed backup must not block saving

    # ------------------------------------------------------------- profile
    def update_profile(self, profile: Profile) -> Config | None:
        """Write the profile section. Only fields whose values changed are touched, so their comments and
        formatting survive and an unchanged save leaves the file byte-identical."""

        def change(doc: CommentedMap) -> None:
            if "profile" not in doc:
                doc["profile"] = CommentedMap()
            section = doc["profile"]
            current = Profile.model_validate(_interpolate(_plain(section)))
            if current == profile:
                raise _NoChange
            if current.min_score != profile.min_score:
                section["min_score"] = profile.min_score
            if current.interests != profile.interests:
                interests = CommentedSeq()
                for i in profile.interests:
                    item = CommentedMap(topic=DoubleQuotedScalarString(i.topic), weight=i.weight)
                    item.fa.set_flow_style()  # one line each: - {topic: "...", weight: 4}
                    interests.append(item)
                section["interests"] = interests
            if current.dislikes != profile.dislikes:
                section["dislikes"] = CommentedSeq(profile.dislikes)
            if current.exclude_keywords != profile.exclude_keywords:
                section["exclude_keywords"] = CommentedSeq(profile.exclude_keywords)
            if current.notes.strip() != profile.notes.strip():
                notes = profile.notes.strip()
                section["notes"] = LiteralScalarString(notes + "\n") if notes else ""

        return self.edit(change)

    # ------------------------------------------------------------- sources
    @staticmethod
    def _find_source(doc: CommentedMap, url: str) -> tuple[CommentedSeq, int | None]:
        seq = doc.get("sources")
        if seq is None:
            seq = CommentedSeq()
            doc["sources"] = seq
        for i, item in enumerate(seq):
            if isinstance(item, dict) and str(item.get("url", "")).rstrip("/") == url.rstrip("/"):
                return seq, i
        return seq, None

    def add_source(self, name: str, url: str, kind: str, options: dict[str, Any] | None = None) -> Config:
        def change(doc: CommentedMap) -> None:
            seq, idx = self._find_source(doc, url)
            entry = CommentedMap(name=name, url=url, kind=kind)
            if options:
                entry["options"] = CommentedMap(options)
            if idx is None:
                seq.append(entry)
            else:
                seq[idx].update(entry)
                seq[idx].pop("enabled", None)

        return self.edit(change)

    def has_source(self, url: str) -> bool:
        try:
            doc = _yaml().load(self.read_text()) or CommentedMap()
        except YAMLError:
            return False
        return self._find_source(doc, url)[1] is not None

    def set_source_enabled(self, url: str, enabled: bool) -> bool:
        """Returns False (and leaves the file alone) if the URL isn't a source in the file."""

        def change(doc: CommentedMap) -> None:
            seq, idx = self._find_source(doc, url)
            if idx is None:
                raise _NoChange
            if enabled:
                seq[idx].pop("enabled", None)
            else:
                seq[idx]["enabled"] = False

        return self.edit(change) is not None

    def remove_source(self, url: str) -> bool:
        def change(doc: CommentedMap) -> None:
            seq, idx = self._find_source(doc, url)
            if idx is None:
                raise _NoChange
            del seq[idx]

        return self.edit(change) is not None

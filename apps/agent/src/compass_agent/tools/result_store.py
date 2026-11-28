"""Private tool originals and bounded, progressive observations.

The caller owns authorization and accepted manifests. This storage layer receives
trusted session coordinates and never resolves a user-supplied filesystem path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid5

from compass_agent.tools.contracts import ToolRejected, json_bytes

INLINE_TOKENS = 20_000
RECEIPT_TOKENS = 512
_REF = re.compile(r"results/r_([0-9a-f]{32})_([0-9a-f]{32})\.(jsonl|json|txt)\Z")


def byte_tokens(value: str) -> int:
    """UTF-8 bytes are a conservative token upper bound when no tokenizer is bound."""
    return len(value.encode("utf-8"))


def source_run(ref: str) -> UUID:
    match = _REF.fullmatch(ref)
    if match is None:
        raise ToolRejected("result_reference_invalid")
    return UUID(hex=match[1])


def encode_cursor(ref: str, digest: str, offset: int, within: int, section: str = "rows") -> str:
    return (
        base64.urlsafe_b64encode(json_bytes([ref, digest, offset, within, section]))
        .decode()
        .rstrip("=")
    )


def decode_cursor(cursor: str, ref: str, digest: str) -> tuple[int, int, str]:
    try:
        if len(cursor) > 1024:
            raise ValueError
        value = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            len(value) != 5
            or value[:2] != [ref, digest]
            or value[4] not in {"rows", "metadata"}
            or any(type(v) is not int or v < 0 for v in value[2:4])
        ):
            raise ValueError
        return value[2], value[3], value[4]
    except (ValueError, TypeError, IndexError) as error:
        raise ToolRejected("result_cursor_invalid") from error


class ResultWriter:
    def __init__(self, store, session_id, run_id, step_id, call_id, format, columns=()):
        self.store, self.session_id, self.run_id = store, session_id, run_id
        self.step_id, self.call_id, self.format = step_id, call_id, format
        self.columns = list(columns)
        self.metadata = {}
        self.header_written = False
        directory = store.directory(session_id)
        descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=directory)
        self.path = Path(name)
        self.file = os.fdopen(descriptor, "wb")
        self.digest = hashlib.sha256()
        self.byte_size = self.count = 0
        self.samples: list = []
        self.finished = False

    def write(self, data: bytes):
        self.file.write(data)
        self.digest.update(data)
        self.byte_size += len(data)

    def header(self):
        if self.format == "jsonl" and not self.header_written:
            self.write(json_bytes({**self.metadata, "columns": self.columns}) + b"\n")
            self.header_written = True

    def row(self, row: object):
        self.header()
        data = json_bytes(row)
        self.write(data + b"\n")
        self.count += 1
        # Preview memory stays bounded even for a single very large field.
        if len(self.samples) < 5 and len(data) <= INLINE_TOKENS:
            self.samples.append(row)

    def finish(self, *, complete=True, source_deps=()) -> dict:
        self.header()
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        checksum = self.digest.hexdigest()
        identity = uuid5(self.run_id, f"{self.step_id}:{self.call_id}:{checksum}")
        ref = f"results/r_{self.run_id.hex}_{identity.hex}.{self.format}"
        destination = self.store.path(self.session_id, ref)
        os.replace(self.path, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.finished = True
        return {
            "schemaVersion": 1,
            "ref": ref,
            "sessionId": str(self.session_id),
            "sourceRunId": str(self.run_id),
            "stepId": str(self.step_id),
            "toolCallId": self.call_id,
            "format": self.format,
            "rowHeader": self.format == "jsonl",
            "bytes": self.byte_size,
            "sha256": checksum,
            "returnedCount": self.count,
            "complete": complete,
            "columns": self.columns,
            "preview": self.samples,
            "sourceDeps": list(source_deps),
        }

    def close(self):
        if not self.file.closed:
            self.file.close()
        if not self.finished:
            self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ToolResultStore:
    def __init__(self, root: str | Path, *, count_tokens: Callable[[str], int] = byte_tokens):
        self.root = Path(root)
        self.count_tokens = count_tokens

    def directory(self, session_id: UUID) -> Path:
        directory = self.root / UUID(str(session_id)).hex
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory

    def path(self, session_id: UUID, ref: str) -> Path:
        source_run(ref)
        return self.root / UUID(str(session_id)).hex / ref.removeprefix("results/")

    def writer(self, session_id, run_id, step_id, call_id, format="jsonl", *, columns=()):
        return ResultWriter(self, session_id, run_id, step_id, call_id, format, columns)

    def save(self, value, *, session_id, run_id, step_id, call_id, source_deps=()) -> dict:
        table = isinstance(value, dict) and isinstance(value.get("rows"), (list, tuple))
        format = "jsonl" if table else "txt" if isinstance(value, str) else "json"
        columns = value.get("columns", ()) if isinstance(value, dict) and table else ()
        with self.writer(session_id, run_id, step_id, call_id, format, columns=columns) as writer:
            if isinstance(value, dict) and table:
                writer.metadata = {k: v for k, v in value.items() if k not in {"columns", "rows"}}
                for row in value["rows"]:
                    writer.row(row)
            elif isinstance(value, str):
                writer.write(value.encode())
                writer.count = len(value)
            else:
                encoder = json.JSONEncoder(
                    ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
                for part in encoder.iterencode(value):
                    writer.write(part.encode())
                writer.count = len(value) if isinstance(value, (dict, list)) else 1
            return writer.finish(
                complete=not bool(isinstance(value, dict) and value.get("truncated")),
                source_deps=source_deps,
            )

    def receipt(self, manifest: dict, *, status="ok", metadata: dict | None = None) -> dict:
        result = {
            "status": status,
            "ref": manifest["ref"],
            "complete": manifest["complete"],
            "originalComplete": manifest["complete"],
            "truncated": not manifest["complete"],
            "previewTruncated": True,
            "preview": [],
            "returnedRows" if manifest["format"] == "jsonl" else "returnedCount": manifest[
                "returnedCount"
            ],
        }
        # Result identity and upstream errors remain meaningful after spilling.
        for key, value in (metadata or {}).items():
            if key in {"resultId", "code", "semantic_result_ref", "calculation_mode"}:
                candidate = {**result, key: value}
                if self.count_tokens(json_bytes(candidate).decode()) <= RECEIPT_TOKENS:
                    result = candidate
        if manifest["columns"]:
            candidate = {**result, "columns": manifest["columns"]}
            if self.count_tokens(json_bytes(candidate).decode()) <= RECEIPT_TOKENS:
                result = candidate
            else:
                candidate = {**result, "columnsInOriginal": True}
                if self.count_tokens(json_bytes(candidate).decode()) <= RECEIPT_TOKENS:
                    result = candidate
        for row in manifest["preview"]:
            candidate = {**result, "preview": [*result["preview"], row]}
            if self.count_tokens(json_bytes(candidate).decode()) > RECEIPT_TOKENS:
                break
            result = candidate
        if manifest["format"] == "jsonl":
            result["previewTruncated"] = len(result["preview"]) < manifest["returnedCount"]
        return result

    def read(self, manifest: dict, *, offset=0, limit=None, cursor=None, budget=INLINE_TOKENS):
        ref = manifest["ref"]
        session_id = UUID(manifest["sessionId"])
        path = self.path(session_id, ref)
        try:
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["sha256"]:
                    raise ToolRejected("result_file_changed")
            if manifest["format"] == "jsonl":
                return self._read_rows(path, manifest, offset, limit, cursor, budget)
            return self._read_text(path, manifest, offset, limit, cursor, budget)
        except OSError as error:
            raise ToolRejected("result_file_unavailable") from error

    def _page(self, manifest, data, start, end, next_position):
        return {
            **({"metadata": manifest["pageMetadata"]} if "pageMetadata" in manifest else {}),
            "ref": manifest["ref"],
            "range": {"start": start, "end": end},
            "data": data,
            "nextCursor": encode_cursor(manifest["ref"], manifest["sha256"], *next_position)
            if next_position is not None
            else None,
            "complete": next_position is None,
            "sourceComplete": manifest["complete"],
            "returnedRows" if manifest["format"] == "jsonl" else "returnedCount": manifest[
                "returnedCount"
            ],
        }

    def _read_rows(self, path, manifest, offset, limit, cursor, budget):
        within, section = 0, "rows"
        if cursor:
            offset, within, section = decode_cursor(cursor, manifest["ref"], manifest["sha256"])
        if manifest.get("rowHeader"):
            with path.open(encoding="utf-8") as stream:
                header = stream.readline().rstrip("\n")
            if self.count_tokens(header) <= 256:
                manifest = {**manifest, "pageMetadata": json.loads(header)}
            elif not cursor or section == "metadata":
                return self._metadata_fragment(manifest, header, offset, within, budget)
        if offset >= manifest["returnedCount"]:
            return self._page(manifest, [], offset, offset, None)
        remaining = limit if limit is not None else manifest["returnedCount"]
        rows, end = [], offset
        with path.open(encoding="utf-8") as stream:
            if manifest.get("rowHeader"):
                stream.readline()
            for index, line in enumerate(stream):
                if index < offset:
                    continue
                line = line.rstrip("\n")
                next_position = (index + 1, 0) if index + 1 < manifest["returnedCount"] else None
                row = json.loads(line)
                candidate = self._page(manifest, [*rows, row], offset, index + 1, next_position)
                if not within and self.count_tokens(json_bytes(candidate).decode()) <= budget:
                    rows.append(row)
                    end = index + 1
                    remaining -= 1
                    if remaining == 0:
                        return candidate
                    continue
                if rows:
                    return self._page(manifest, rows, offset, end, (index, 0))
                # A single record may exceed a page. Keep its exact JSON text and
                # advance in characters; the next page continues the same record.
                return self._fragment(manifest, line, offset, within, budget, next_position)
        return self._page(manifest, rows, offset, end, None)

    def _metadata_fragment(self, manifest, text, offset, within, budget):
        if within >= len(text):
            raise ToolRejected("result_cursor_invalid")
        low, high, best = within + 1, len(text), None
        while low <= high:
            end = (low + high) // 2
            next_position = (offset, end, "metadata") if end < len(text) else (offset, 0, "rows")
            candidate = self._page(
                manifest,
                {
                    "metadataFragment": text[within:end],
                    "characterStart": within,
                    "characterEnd": end,
                },
                offset,
                offset,
                next_position,
            )
            if self.count_tokens(json_bytes(candidate).decode()) <= budget:
                best, low = candidate, end + 1
            else:
                high = end - 1
        if best is None:
            raise ToolRejected("result_page_budget_unavailable")
        return best

    def _fragment(self, manifest, text, row, within, budget, next_position):
        if within >= len(text):
            raise ToolRejected("result_cursor_invalid")
        low, high, best = within + 1, len(text), None
        while low <= high:
            end = (low + high) // 2
            candidate = self._page(
                manifest,
                {
                    "row": row,
                    "characterStart": within,
                    "characterEnd": end,
                    "fragment": text[within:end],
                },
                row,
                row + (end == len(text)),
                (row, end) if end < len(text) else next_position,
            )
            if self.count_tokens(json_bytes(candidate).decode()) <= budget:
                best, low = candidate, end + 1
            else:
                high = end - 1
        if best is None:
            raise ToolRejected("result_page_budget_unavailable")
        return best

    def _read_text(self, path, manifest, offset, limit, cursor, budget):
        if cursor:
            offset, within, section = decode_cursor(cursor, manifest["ref"], manifest["sha256"])
            if within or section != "rows":
                raise ToolRejected("result_cursor_invalid")
        # Seek by characters, preserving UTF-8 boundaries without loading the file.
        with path.open(encoding="utf-8") as stream:
            remaining = offset
            while remaining:
                part = stream.read(min(remaining, 8192))
                if not part:
                    return self._page(manifest, "", offset, offset, None)
                remaining -= len(part)
            text = stream.read(min(limit or budget, budget) + 1)
        low, high, best = 0, min(len(text), limit or budget), None
        while low <= high:
            length = (low + high) // 2
            next_position = (offset + length, 0) if length < len(text) else None
            candidate = self._page(manifest, text[:length], offset, offset + length, next_position)
            if self.count_tokens(json_bytes(candidate).decode()) <= budget:
                best, low = candidate, length + 1
            else:
                high = length - 1
        if best is None or text and best["range"]["end"] == offset:
            raise ToolRejected("result_page_budget_unavailable")
        return best

    def remove_session(self, session_id: UUID):
        directory = self.root / UUID(str(session_id)).hex
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file():
                    path.unlink()
            directory.rmdir()

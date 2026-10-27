# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture offline CSV records one row at a time as retrievable evidence."""

from __future__ import annotations

import csv
import io
import json
from typing import Final
from urllib.parse import quote

from pydantic import BaseModel, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME: Final = "csv-row"
CSV_ROW_SOURCE_VERSION: Final = "1"

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_VERSION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]


class CSVDocumentInput(BaseModel):
    """Complete offline CSV bytes plus the document-level row identity keys."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One validated CSV record in its original header order."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured CSV record addressed by document and primary-key value."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="after")
    def validate_identity(self) -> CSVRowSource:
        expected = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.name != expected or self.source_id != expected:
            raise ValueError("CSV row Source must use its row identity for name and source_id")  # noqa: TRY003
        return self


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the delimiter-safe, bounded identity of one CSV record."""

    _validate_document_id(document_id)
    _validate_primary_key_value(primary_key_value)
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row source id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and fully validate one CSV document before returning its rows."""

    _validate_document_id(value.document_id)
    _validate_primary_key(value.primary_key)
    records = _read_records(value.content)
    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003
    columns = tuple(records[0])
    _validate_header(columns)
    try:
        key_index = columns.index(value.primary_key)
    except ValueError:
        raise ValueError("primary_key must exactly match one header column") from None  # noqa: TRY003

    rows: list[CSVRowInput] = []
    seen_values: set[str] = set()
    for record in records[1:]:
        if not record:
            # A completely empty physical record carries no fields and is skipped.
            continue
        if len(record) != len(columns):
            raise ValueError("CSV record width must match the header width")  # noqa: TRY003
        primary_key_value = record[key_index]
        _validate_primary_key_value(primary_key_value)
        if primary_key_value in seen_values:
            raise ValueError("primary_key values must be unique within one document")  # noqa: TRY003
        seen_values.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )
    return tuple(rows)


def _read_records(content: bytes) -> list[list[str]]:
    if not isinstance(content, bytes | bytearray | memoryview):
        raise ValueError("CSV content must be bytes")  # noqa: TRY003, TRY004
    try:
        text = bytes(content).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be strict UTF-8") from error  # noqa: TRY003
    try:
        return list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as error:
        raise ValueError("CSV content must use well-formed quoting") from error  # noqa: TRY003


def _validate_header(columns: tuple[str, ...]) -> None:
    if not columns:
        raise ValueError("CSV header must contain at least one column")  # noqa: TRY003
    seen: set[str] = set()
    for column in columns:
        if not column.strip():
            raise ValueError("CSV header columns must not be empty or blank")  # noqa: TRY003
        if column in seen:
            raise ValueError("CSV header columns must be unique")  # noqa: TRY003
        seen.add(column)


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty string without surrounding whitespace")  # noqa: TRY003


def _validate_primary_key(primary_key: str) -> None:
    if not isinstance(primary_key, str) or not primary_key:
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003


def _validate_primary_key_value(primary_key_value: str) -> None:
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("primary_key_value must not be empty or blank")  # noqa: TRY003


def _row_mapping(source: CSVRowSource) -> dict[str, str]:
    return dict(zip(source.columns, source.values, strict=True))


class CSVRowSourceAdapter:
    """Resolve and read one validated CSV record without altering its values."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        _validate_row_input(value)
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


def _validate_row_input(value: CSVRowInput) -> None:
    _validate_document_id(value.document_id)
    _validate_primary_key(value.primary_key)
    _validate_primary_key_value(value.primary_key_value)
    if not value.columns:
        raise ValueError("CSV row columns must not be empty")  # noqa: TRY003
    if len(value.columns) != len(value.values):
        raise ValueError("CSV row columns and values must have the same length")  # noqa: TRY003
    seen: set[str] = set()
    for column in value.columns:
        if not column.strip():
            raise ValueError("CSV row columns must not be empty or blank")  # noqa: TRY003
        if column in seen:
            raise ValueError("CSV row columns must be unique")  # noqa: TRY003
        seen.add(column)
    if value.primary_key not in seen:
        raise ValueError("primary_key must exactly match one row column")  # noqa: TRY003


class CSVRowTextEvidenceProjection:
    """Expose one complete CSV record as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = _row_mapping(source)
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_SOURCE_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)

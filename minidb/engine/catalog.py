"""The system catalog: table schemas and where their data lives.

The catalog is stored inside the database file itself (so it is covered by the
same WAL/rollback guarantees as user data). It is serialized to JSON and
written across a linked chain of pages rooted at ``meta.catalog_root``.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Optional

from ..storage.pager import NO_PAGE, PAGE_SIZE, Pager

_CHUNK = PAGE_SIZE - 8  # 4 bytes next-page + 4 bytes chunk length


@dataclass
class TableSchema:
    name: str
    columns: list[tuple[str, str]]  # ordered (name, type)
    pk: str
    not_null: list[str] = field(default_factory=list)
    root_page_id: int = NO_PAGE

    @property
    def column_names(self) -> list[str]:
        return [c[0] for c in self.columns]

    def type_of(self, column: str) -> str:
        for name, col_type in self.columns:
            if name == column:
                return col_type
        raise KeyError(column)

    def has_column(self, column: str) -> bool:
        return any(name == column for name, _ in self.columns)


class Catalog:
    def __init__(self, pager: Pager):
        self.pager = pager
        self.tables: dict[str, TableSchema] = {}
        if pager.meta.catalog_root != NO_PAGE:
            self._load()

    # -- serialization to a page chain ------------------------------------
    def _load(self) -> None:
        page_id = self.pager.meta.catalog_root
        buf = bytearray()
        while page_id != NO_PAGE:
            page = bytes(self.pager.read_page(page_id))
            next_page, length = struct.unpack_from("<iI", page, 0)
            buf += page[8 : 8 + length]
            page_id = next_page
        raw = json.loads(buf.decode("utf-8"))
        for name, spec in raw.items():
            self.tables[name] = TableSchema(
                name=name,
                columns=[tuple(c) for c in spec["columns"]],
                pk=spec["pk"],
                not_null=spec.get("not_null", []),
                root_page_id=spec["root_page_id"],
            )

    def save(self) -> None:
        raw = {
            name: {
                "columns": [list(c) for c in t.columns],
                "pk": t.pk,
                "not_null": t.not_null,
                "root_page_id": t.root_page_id,
            }
            for name, t in self.tables.items()
        }
        data = json.dumps(raw).encode("utf-8")
        # free the previous chain so pages are reused
        self._free_chain(self.pager.meta.catalog_root)

        chunks = [data[i : i + _CHUNK] for i in range(0, len(data), _CHUNK)] or [b""]
        page_ids = [self.pager.allocate_page() for _ in chunks]
        for idx, (pid, chunk) in enumerate(zip(page_ids, chunks)):
            next_page = page_ids[idx + 1] if idx + 1 < len(page_ids) else NO_PAGE
            page = bytearray(PAGE_SIZE)
            struct.pack_into("<iI", page, 0, next_page, len(chunk))
            page[8 : 8 + len(chunk)] = chunk
            self.pager.write_page(pid, page)
        self.pager.meta.catalog_root = page_ids[0]

    def _free_chain(self, page_id: int) -> None:
        while page_id != NO_PAGE:
            page = bytes(self.pager.read_page(page_id))
            (next_page,) = struct.unpack_from("<i", page, 0)
            self.pager.free_page(page_id)
            page_id = next_page

    # -- lookups -----------------------------------------------------------
    def get(self, name: str) -> Optional[TableSchema]:
        return self.tables.get(name)

    def add(self, schema: TableSchema) -> None:
        self.tables[schema.name] = schema

    def drop(self, name: str) -> None:
        del self.tables[name]

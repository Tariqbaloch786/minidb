"""Page checksums and corruption detection at the physical pager layer."""

import pytest

from minidb.storage.pager import (
    DATA_SIZE,
    PAGE_SIZE,
    CorruptionError,
    Pager,
)


@pytest.fixture
def pager(tmp_path):
    p = Pager(str(tmp_path / "c.db"))
    yield p
    try:
        p.close()
    except Exception:
        pass


def _flip(path, offset):
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([b[0] ^ 0xFF]))


def test_roundtrip_is_stable(pager):
    pid = pager.allocate_page()
    payload = bytes(range(256)) * (DATA_SIZE // 256) + b"\x00" * (DATA_SIZE % 256)
    pager.write_page(pid, payload)
    pager.sync()
    assert bytes(pager.read_page(pid)) == payload


def test_single_byte_corruption_in_data(tmp_path):
    path = str(tmp_path / "c.db")
    p = Pager(path)
    pid = p.allocate_page()
    p.write_page(pid, b"A" * DATA_SIZE)
    p.close()
    _flip(path, pid * PAGE_SIZE + 8 + 100)  # a byte inside the data area
    p2 = Pager(path)
    with pytest.raises(CorruptionError):
        p2.read_page(pid)
    p2.close()


def test_corruption_in_checksum_field(tmp_path):
    path = str(tmp_path / "c.db")
    p = Pager(path)
    pid = p.allocate_page()
    p.write_page(pid, b"B" * DATA_SIZE)
    p.close()
    _flip(path, pid * PAGE_SIZE + 0)  # the crc32 field itself
    p2 = Pager(path)
    with pytest.raises(CorruptionError):
        p2.read_page(pid)
    p2.close()


def test_corruption_in_page_id_field(tmp_path):
    path = str(tmp_path / "c.db")
    p = Pager(path)
    pid = p.allocate_page()
    p.write_page(pid, b"C" * DATA_SIZE)
    p.close()
    _flip(path, pid * PAGE_SIZE + 4)  # the page-id field
    p2 = Pager(path)
    with pytest.raises(CorruptionError):
        p2.read_page(pid)
    p2.close()


def test_misdirected_page_detected(tmp_path):
    """A whole page written at the wrong offset is caught by the id check."""
    path = str(tmp_path / "c.db")
    p = Pager(path)
    a = p.allocate_page()
    b = p.allocate_page()
    p.write_page(a, b"a" * DATA_SIZE)
    p.write_page(b, b"b" * DATA_SIZE)
    p.sync()
    # copy page a's raw bytes over page b's slot
    with open(path, "r+b") as f:
        f.seek(a * PAGE_SIZE)
        raw = f.read(PAGE_SIZE)
        f.seek(b * PAGE_SIZE)
        f.write(raw)
    p.close()
    p2 = Pager(path)
    with pytest.raises(CorruptionError):
        p2.read_page(b)  # carries id `a`, not `b`
    p2.close()


def test_zeroed_page_is_corruption(tmp_path):
    """An all-zero page (e.g. a hole) is not a valid page."""
    path = str(tmp_path / "c.db")
    p = Pager(path)
    pid = p.allocate_page()
    p.write_page(pid, b"D" * DATA_SIZE)
    p.close()
    with open(path, "r+b") as f:
        f.seek(pid * PAGE_SIZE)
        f.write(b"\x00" * PAGE_SIZE)
    p2 = Pager(path)
    with pytest.raises(CorruptionError):
        p2.read_page(pid)
    p2.close()


def test_truncated_file(tmp_path):
    path = str(tmp_path / "c.db")
    p = Pager(path)
    pid = p.allocate_page()
    p.write_page(pid, b"E" * DATA_SIZE)
    p.sync()
    p.close()
    with open(path, "r+b") as f:
        f.truncate(pid * PAGE_SIZE + 10)  # cut this page short
    p2 = Pager(path)
    with pytest.raises(CorruptionError):
        p2.read_page(pid)
    p2.close()


def test_invalid_page_ids(pager):
    with pytest.raises(CorruptionError):
        pager.read_page(-1)
    with pytest.raises(CorruptionError):
        pager.read_page(9999)  # past end of file


def test_meta_page_is_checksum_protected(tmp_path):
    path = str(tmp_path / "c.db")
    Pager(path).close()  # creates a valid meta page 0
    _flip(path, 8 + 4)  # corrupt a byte in page 0's data area
    with pytest.raises(CorruptionError):
        Pager(path)  # opening re-reads (and verifies) the meta page

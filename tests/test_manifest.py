import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.awb import manifest
from app.awb.router import _dump
from app.dedup.pg_store import _map_awb
from app.warehouse import warehouse_context


@pytest.mark.parametrize("warehouse", ["cas", "bgd"])
def test_lookup_parameterized_and_warehouse(monkeypatch, warehouse):
    cursor = MagicMock()
    cursor.fetchone.return_value = {key: None for key in (manifest.CAS_FIELDS if warehouse == "cas" else manifest.FIELDS)}
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    connect = MagicMock(return_value=connection)
    monkeypatch.setitem(sys.modules, "pymysql", SimpleNamespace(connect=connect, cursors=SimpleNamespace(DictCursor=object)))
    monkeypatch.setattr(manifest, "dotenv_values", lambda _: {})
    for site in ("CAS", "BGD"):
        for key, value in {"HOST": site, "USER": "reader", "PASSWORD": "secret", "NAME": site}.items():
            monkeypatch.setenv(f"MANIFEST_{site}_DB_{key}", value)
        monkeypatch.delenv(f"MANIFEST_{site}_DB_READONLY_HOST", raising=False)
    manifest._lookup(warehouse, "M' OR 1=1", "H' OR 1=1")
    assert connect.call_args.kwargs["host"] == warehouse.upper()
    sql, params = cursor.execute.call_args.args
    if warehouse == "cas":
        assert "`tpsonline_cas`.`nvocc_detil`" in sql
        assert "`NO_MASTER_BL_AWB` = %s AND `no_host_bl_awb` = %s" in sql
        assert "BINARY" not in sql  # BINARY menonaktifkan indeks pada tabel 1.6 jt baris
    else:
        assert "WHERE BINARY MasterAWB = BINARY %s AND BINARY HostAWB = BINARY %s" in sql
    assert "OR 1=1" not in sql
    assert params == ("M' OR 1=1", "H' OR 1=1")
    connection.close.assert_called_once()


def test_cas_manifest_returns_verified_fields(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = {key: None for key in manifest.CAS_FIELDS}
    cursor.fetchone.return_value.update(NO_MASTER_BL_AWB="M1", NO_HOST_BL_AWB="H1", JUMLAH_KEMASAN="5", PELABUHAN_ASAL="CGK")
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr(manifest, "_connect", lambda _: connection)
    row = manifest._lookup("cas", "M1", "H1")
    assert (row["mawb"], row["hawb"], row["quantity"], row["origin"]) == ("M1", "H1", "5", "CGK")
    assert cursor.execute.call_args.args[1] == ("M1", "H1")


@pytest.mark.parametrize("warehouse", ["cas", "bgd"])
def test_mawb_candidates_use_warehouse_table_and_parameter(monkeypatch, warehouse):
    cursor = MagicMock()
    cursor.fetchall.return_value = [{"mawb": b"MASTER-1"}]
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr(manifest, "_connect", lambda _: connection)
    assert manifest._mawb_lookup(warehouse, "H' OR 1=1") == ["MASTER-1"]
    sql, params = cursor.execute.call_args.args
    assert ("`tpsonline_cas`.`nvocc_detil`" if warehouse == "cas" else "imp_hostawb") in sql
    assert params == ("H' OR 1=1",)
    assert "OR 1=1" not in sql
    connection.close.assert_called_once()


async def test_statuses(monkeypatch):
    assert (await manifest.fetch_manifest("cas", "M", None))["status"] == "missing_numbers"
    monkeypatch.setattr(manifest, "_lookup", lambda *args: None)
    assert (await manifest.fetch_manifest("cas", "M", "H"))["status"] == "not_found"
    def fail(*args):
        raise RuntimeError("secret")
    monkeypatch.setattr(manifest, "_lookup", fail)
    assert await manifest.fetch_manifest("cas", "M", "H") == {"status": "unavailable", "data": None}
    monkeypatch.setattr(manifest, "_lookup", lambda *args: {"mawb": "M"})
    assert (await manifest.fetch_manifest("bgd", "M", "H"))["status"] == "found"


async def test_sqlite_hawb_isolation_and_api(store):
    token = warehouse_context.set("cas")
    try:
        record = await store.insert_awb("M")
        store._connect().execute("UPDATE awb SET hawb_number=? WHERE id=?", ("H", record.id))
        store._connect().commit()
        assert _dump((await store.list_awbs())[0])["hawb_number"] == "H"
    finally:
        warehouse_context.reset(token)
    assert await store.list_awbs() == []


def test_pg_mapping_hawb():
    row = dict(id=1, awb_number="M", hawb_number="H", batch_id=None, status="WAITING", parcel_id=None, created_at=None, part=2, total=5)
    dumped = _dump(_map_awb(row))
    assert dumped["hawb_number"] == "H"
    assert dumped["part"] == 2 and dumped["total"] == 5

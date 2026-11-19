"""Read-only manifest lookup; optional SSH tunnels are managed by the app lifespan."""

import asyncio
import os

from dotenv import dotenv_values

FIELDS = {
    "MasterAWB": "mawb", "HostAWB": "hawb", "airlinescode": "airlines_code",
    "FlightNo": "flight_no", "DateOfFlight": "date_of_flight", "Origin": "origin",
    "Quantity": "quantity", "Weight": "weight", "Volume": "volume", "HSCode": "hs_code",
    "DescriptionGoods": "description_goods", "shippername": "shipper_name",
    "shippercity": "shipper_city", "Consigneename": "consignee_name",
    "Consigneecity": "consignee_city", "Consigneecountry": "consignee_country",
    "bc11": "bc11", "tglbc": "tgl_bc", "SPPB": "sppb", "TGLSPPB": "tgl_sppb",
    "DateEntry": "date_entry", "TimeEntry": "time_entry", "BagNumber": "bag_number",
}
CAS_FIELDS = {
    "NO_MASTER_BL_AWB": "mawb", "NO_HOST_BL_AWB": "hawb",
    "TGL_MASTER_BL_AWB": "mawb_date", "TGL_HOST_BL_AWB": "hawb_date",
    "PELABUHAN_ASAL": "origin", "PELABUHAN_AKHIR": "destination",
    "JUMLAH_KEMASAN": "quantity", "JENIS_KEMASAN": "package_type",
    "BRUTO": "weight", "VOLUME": "volume", "NAMA_SHIPPER": "shipper_name",
    "ALMT_SHIPPER": "shipper_address", "NEG_SHIPPER": "shipper_country",
    "NAMA_CONSIGNEE": "consignee_name", "ALMT_CONSIGNEE": "consignee_address",
    "NEG_CONSIGNEE": "consignee_country", "NOMOR_AJU": "nomor_aju",
    "NO_POS": "no_pos", "NO_SUB_POS": "no_sub_pos", "STATUS_DETIL": "status_detail",
}


def _connect(warehouse):
    import pymysql

    if warehouse not in ("cas", "bgd"):
        raise ValueError("invalid warehouse")
    env = {**dotenv_values(".env"), **os.environ}
    prefix = f"MANIFEST_{warehouse.upper()}_DB_"
    host = env.get(prefix + "READONLY_HOST") or env.get(prefix + "HOST")
    user, password, database = (env.get(prefix + key) for key in ("USER", "PASSWORD", "NAME"))
    if not all((host, user, password, database)):
        raise ValueError("manifest configuration incomplete")
    timeout = float(env.get("MANIFEST_CONNECT_TIMEOUT", "5"))
    if not 0 < timeout <= 60:
        raise ValueError("invalid manifest timeout")
    options = dict(host=host, port=int(env.get(prefix + "PORT", "3306")), user=user,
                   password=password, database=database, connect_timeout=timeout,
                   read_timeout=timeout, write_timeout=timeout,
                   cursorclass=pymysql.cursors.DictCursor)
    enabled = (env.get(f"MANIFEST_{warehouse.upper()}_TUNNEL_ENABLED") or "false").lower()
    if enabled == "false":
        return pymysql.connect(**options)
    if enabled != "true":
        raise ValueError("invalid manifest tunnel configuration")
    from ..manifest_tunnels import _config
    from ..tunnel_manager import manager

    config = _config(env, warehouse.upper())
    if config is None or config["local_port"] != options["port"]:
        raise ValueError("manifest tunnel destination mismatch")
    conn = pymysql.connect(**options, defer_connect=True)
    sock = manager.open_socket(warehouse, timeout=timeout)
    try:
        conn.connect(sock=sock)
    except BaseException:
        sock.close()
        raise
    return conn


def _lookup(warehouse, mawb, hawb):
    conn = _connect(warehouse)
    try:
        with conn.cursor() as cursor:
            if warehouse == "cas":
                # BINARY mematikan indeks NO_MASTER_BL_AWB/NO_HOST_BL_AWB (1.6 jt baris)
                # sehingga query full-scan sampai timeout; indeks sudah case-insensitive
                # (utf8 collation), pencocokan tanpa BINARY tetap tepat untuk data ini.
                cursor.execute(
                    "SELECT " + ", ".join(f"`{column}`" for column in CAS_FIELDS) + " "
                    "FROM `tpsonline_cas`.`nvocc_detil` "
                    "WHERE `NO_MASTER_BL_AWB` = %s AND `no_host_bl_awb` = %s LIMIT 1",
                    (mawb, hawb),
                )
                row = cursor.fetchone()
                return {name: row[column] for column, name in CAS_FIELDS.items()} if row else None
            cursor.execute(
                "SELECT " + ", ".join(f"`{column}`" for column in FIELDS)
                + " FROM imp_hostawb WHERE BINARY MasterAWB = BINARY %s AND BINARY HostAWB = BINARY %s"
                + " ORDER BY noid DESC LIMIT 1", (mawb, hawb),
            )
            row = cursor.fetchone()
            return {name: row[column] for column, name in FIELDS.items()} if row else None
    finally:
        conn.close()


async def fetch_manifest(warehouse, mawb, hawb):
    if not mawb or not mawb.strip() or not hawb or not hawb.strip():
        return {"status": "missing_numbers", "data": None}
    try:
        row = await asyncio.to_thread(_lookup, warehouse, mawb.strip(), hawb.strip())
    except Exception:
        return {"status": "unavailable", "data": None}
    return {"status": "found" if row is not None else "not_found", "data": row}


def _mawb_lookup(warehouse, hawb):
    """Distinct MAWB milik satu HAWB menurut manifest (read-only)."""
    conn = _connect(warehouse)
    try:
        with conn.cursor() as cursor:
            if warehouse == "cas":
                sql = ("SELECT DISTINCT `NO_MASTER_BL_AWB` AS mawb "
                       "FROM `tpsonline_cas`.`nvocc_detil` WHERE `no_host_bl_awb` = %s LIMIT 5")
            else:
                sql = ("SELECT DISTINCT BINARY `MasterAWB` AS mawb FROM imp_hostawb "
                       "WHERE BINARY `HostAWB` = BINARY %s LIMIT 5")
            cursor.execute(sql, (hawb,))
            return [
                row["mawb"].decode("utf-8", "replace") if isinstance(row["mawb"], bytes) else str(row["mawb"])
                for row in cursor.fetchall()
            ]
    finally:
        conn.close()


async def mawb_candidates(warehouse, hawb):
    """Kandidat MAWB dari manifest; None bila manifest tidak terjangkau."""
    try:
        return await asyncio.to_thread(_mawb_lookup, warehouse, hawb.strip())
    except Exception:
        return None

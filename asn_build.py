#!/usr/bin/env python3
"""Tạo asn.db (SQLite) từ ip2asn-v4.tsv(.gz) của iptoasn.com — để bot tra nhà mạng khách offline.
Dùng: python3 asn_build.py ip2asn-v4.tsv.gz /root/awgbot/asn.db
"""
import gzip, ipaddress, os, sqlite3, sys


def rows(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8", errors="replace") as f:
        for ln in f:
            p = ln.rstrip("\n").split("\t")
            if len(p) < 5:
                continue
            try:
                a = int(ipaddress.IPv4Address(p[0])); b = int(ipaddress.IPv4Address(p[1])); asn = int(p[2])
            except ValueError:
                continue
            if asn == 0:
                continue
            yield a, b, asn, p[3], p[4][:60]


def build(src, dst):
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.execute("CREATE TABLE asn(start_ip INTEGER PRIMARY KEY, end_ip INTEGER, asn INTEGER, cc TEXT, descr TEXT)")
    c.executemany("INSERT OR REPLACE INTO asn VALUES(?,?,?,?,?)", rows(src))
    n = c.execute("SELECT COUNT(*) FROM asn").fetchone()[0]
    c.commit(); c.close()
    if n < 1000:
        os.remove(tmp)
        raise SystemExit(f"Dữ liệu quá ít ({n} dải) — giữ asn.db cũ")
    os.replace(tmp, dst)
    return n


if __name__ == "__main__":
    print("asn.db:", build(sys.argv[1], sys.argv[2]), "dải IP")

#!/usr/bin/env python3
"""Generate a self-signed TLS certificate for bond_server.py.

Needs the `openssl` command (standard on Linux/macOS; on Windows use
WSL/Git-Bash or install it). Prints the certificate's SHA256 fingerprint,
which clients pin with --tls-fingerprint.

Usage:  python3 gen_cert.py [--out DIR] [--cn NAME] [--days N]
"""
import argparse
import base64
import hashlib
import os
import subprocess
import sys


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"openssl failed:\n{p.stderr}\nIs openssl installed?")
    return p


def main():
    ap = argparse.ArgumentParser(description="Make a self-signed cert for the bond server.")
    ap.add_argument("--out", default=".", help="directory for bond.crt / bond.key")
    ap.add_argument("--cn", default="bond-server")
    ap.add_argument("--days", type=int, default=825)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    crt = os.path.join(args.out, "bond.crt")
    key = os.path.join(args.out, "bond.key")
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
         "-keyout", key, "-out", crt, "-days", str(args.days),
         "-nodes", "-subj", f"/CN={args.cn}"])

    pem = open(crt).read()
    b64 = "".join(l.strip() for l in pem.splitlines() if "-----" not in l)
    fp = hashlib.sha256(base64.b64decode(b64)).hexdigest()
    with open(os.path.join(args.out, "fingerprint.txt"), "w") as f:
        f.write(fp + "\n")

    print(f"certificate: {crt}")
    print(f"key:         {key}")
    print(f"\nSHA256 fingerprint (give this to clients):\n{fp}\n")
    print("Server:  python3 bond_server.py --port 9000 --token SECRET "
          f"--tls-cert {crt} --tls-key {key}")
    print("Client:  py bond_client.py --server VPS_IP --token SECRET "
          f"--tls-fingerprint {fp}")


if __name__ == "__main__":
    main()

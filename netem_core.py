#!/usr/bin/env python3
import subprocess
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TC = "/usr/sbin/tc"
IP = "/usr/sbin/ip"


def run_cmd(cmd):
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def list_interfaces():
    """
    Returner en liste med dicts:
    [
      {"name": "ens18", "has_ip": True/False, "mac": "..."},
      ...
    ]
    """
    rc, out, err = run_cmd(f"{IP} -o link show")
    if rc != 0:
        raise RuntimeError(f"ip link show failed: {err or out}")

    ifaces = []
    for line in out.splitlines():
        # Example line: "2: ens18: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ..."
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        idx = parts[0].strip()
        name = parts[1].strip()
        # Skip lo
        if name == "lo":
            continue

        ifaces.append({
            "index": int(idx),
            "name": name,
        })

    # optional: look up IP
    rc2, out2, err2 = run_cmd(f"{IP} -o addr show")
    addr_map = {}
    if rc2 == 0:
        for line in out2.splitlines():
            # eksempel: "2: ens18    inet 10.0.0.1/24 ..."
            parts = line.split()
            if len(parts) >= 4:
                name = parts[1]
                fam = parts[2]
                addr = parts[3]
                if fam == "inet":
                    addr_map.setdefault(name, []).append(addr)

    for iface in ifaces:
        name = iface["name"]
        ips = addr_map.get(name, [])
        iface["ips"] = ips
        iface["has_ip"] = bool(ips)

    return ifaces


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def ensure_bridge_for_link(link):
    """
    link = {
      "id": "wan1",
      "name": "WAN 1 – Starlink",
      "bridge": "br-wan1",
      "inner": "ens19",
      "outer": "ens21"
    }
    Opprett bro hvis nødvendig, sett inner/outer som master, sett alt up.
    """
    bridge = link["bridge"]
    inner = link["inner"]
    outer = link["outer"]

    # Bridge exists?
    rc, out, err = run_cmd(f"{IP} link show {bridge}")
    if rc != 0:
        # Configure new bridge
        run_cmd(f"{IP} link add {bridge} type bridge")

    # Put ifaces in bridge
    for ifname in (inner, outer):
        run_cmd(f"{IP} link set {ifname} master {bridge}")
        run_cmd(f"{IP} link set {ifname} up")

    # Set up bridge
    run_cmd(f"{IP} link set {bridge} up")


def get_qdisc_raw(iface):
    rc, out, err = run_cmd(f"{TC} qdisc show dev {iface}")
    if rc != 0:
        return err or f"Error (rc={rc})"
    return out or "no qdisc configured"

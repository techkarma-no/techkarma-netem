#!/usr/bin/env python3
import json
import re
import subprocess
from pathlib import Path

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)

app = Flask(__name__)
app.secret_key = "techkarma-netem"  # endre hvis du vil

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

TC = "/usr/sbin/tc"
IP = "/usr/sbin/ip"


# ---------- Helper: shell ----------

def run_cmd(cmd: str):
    """Run command and return (rc, stdout, stderr)."""
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    out, err = proc.communicate()
    return proc.returncode, (out or "").strip(), (err or "").strip()


# ---------- Config ----------

def load_config():
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open() as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg: dict):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_PATH)


# ---------- NIC discovery ----------

def get_all_nics():
    """
    Returns a list of all non-loopback NIC names,
    e.g. ["ens18", "ens19", "ens20", "br-wan1", "br-wan2"].
    """
    rc, out, err = run_cmd(f"{IP} -o link show")
    if rc != 0:
        return []

    nics = []
    for line in out.splitlines():
        # "2: ens18: <BROADCAST,..."
        parts = line.split(": ", 2)
        if len(parts) < 2:
            continue
        name = parts[1].split("@", 1)[0]
        name = name.strip()
        if name == "lo":
            continue
        nics.append(name)
    return sorted(nics)


def guess_mgmt_interface():
    """
    Guess mgmt interface as the first non-loopback NIC with an IPv4 address.
    """
    rc, out, err = run_cmd(f"{IP} -o addr show")
    if rc != 0:
        return None

    candidates = []
    for line in out.splitlines():
        # "2: ens18    inet 10.240.54.8/24 ..."
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            ifname = parts[1].split("@", 1)[0]
            if ifname != "lo":
                candidates.append(ifname)

    return candidates[0] if candidates else None


def get_setup_nics(cfg: dict):
    """
    Return NICs that are eligible for use in WAN 1 / WAN 2:
    - not loopback
    - not mgmt
    - not existing bridges (br-*)
    """
    all_nics = get_all_nics()
    mgmt = cfg.get("mgmt_interface")
    usable = []

    for name in all_nics:
        if name.startswith("br-"):
            continue
        if mgmt and name == mgmt:
            continue
        usable.append(name)

    return usable


# ---------- qdisc parsing / netem ----------

def parse_qdisc_output(raw: str):
    """
    Parse `tc qdisc show dev <if>` into a dict:
    {
      "raw": "...",
      "parsed": {
        "kind": "netem" / "fq_codel" / None,
        "delay_ms": float or None,
        "jitter_ms": float or None,
        "loss_pct": float or None,
        "rate_mbit": float or None,
      }
    }
    """
    info = {
        "raw": raw.strip(),
        "parsed": {
            "kind": None,
            "delay_ms": None,
            "jitter_ms": None,
            "loss_pct": None,
            "rate_mbit": None,
        },
    }

    if not raw.strip():
        return info

    first_line = raw.splitlines()[0]

    # Identify qdisc kind
    m_kind = re.search(r"qdisc\s+(\S+)\s+\d+:", first_line)
    if m_kind:
        kind = m_kind.group(1)
        info["parsed"]["kind"] = kind
    else:
        return info

    if info["parsed"]["kind"] != "netem":
        # we only parse details for netem; others are left with kind only
        return info

    # delay Xms / delay Xms Yms
    m_delay = re.search(r"delay\s+([\d\.]+)ms", first_line)
    if m_delay:
        info["parsed"]["delay_ms"] = float(m_delay.group(1))

    m_delay2 = re.search(r"delay\s+([\d\.]+)ms\s+([\d\.]+)ms", first_line)
    if m_delay2:
        info["parsed"]["jitter_ms"] = float(m_delay2.group(2))

    # loss
    m_loss = re.search(r"loss\s+([\d\.]+)%", first_line)
    if m_loss:
        info["parsed"]["loss_pct"] = float(m_loss.group(1))

    # rate – usually appears in a tbf line
    for line in raw.splitlines():
        m_rate = re.search(r"tbf\s+.*rate\s+([\d\.]+)([KMG])bit", line)
        if m_rate:
            value = float(m_rate.group(1))
            unit = m_rate.group(2).upper()
            if unit == "K":
                value = value / 1000.0
            elif unit == "G":
                value = value * 1000.0
            info["parsed"]["rate_mbit"] = value
            break

    return info


def get_qdisc_state(ifname: str):
    rc, out, err = run_cmd(f"{TC} qdisc show dev {ifname}")
    if rc != 0:
        raw = err or ""
    else:
        raw = out or ""
    return parse_qdisc_output(raw)


def clear_qdisc(ifname: str):
    run_cmd(f"{TC} qdisc del dev {ifname} root")


def apply_netem(ifname: str, delay_ms: float, jitter_ms: float,
                loss_pct: float, rate_mbit: float):
    """
    Apply netem + optional tbf on interface.
    """
    # Always start clean
    clear_qdisc(ifname)

    parts = ["netem"]
    if delay_ms and delay_ms > 0:
        if jitter_ms and jitter_ms > 0:
            parts.append(f"delay {delay_ms:.1f}ms {jitter_ms:.1f}ms")
        else:
            parts.append(f"delay {delay_ms:.1f}ms")

    if loss_pct and loss_pct > 0:
        parts.append(f"loss {loss_pct:.3f}%")

    netem_cmd = f"{TC} qdisc add dev {ifname} root handle 1:0 " + " ".join(parts)
    rc, out, err = run_cmd(netem_cmd)
    if rc != 0:
        return False, f"Failed to apply netem: {err or out or 'unknown error'}"

    if rate_mbit and rate_mbit > 0:
        rate_str = f"{rate_mbit:.3f}mbit"
        tbf_cmd = (
            f"{TC} qdisc add dev {ifname} parent 1:1 handle 10: tbf "
            f"rate {rate_str} buffer 3200 limit 32768"
        )
        rc2, out2, err2 = run_cmd(tbf_cmd)
        if rc2 != 0:
            return False, f"Netem OK, but tbf failed: {err2 or out2 or 'unknown error'}"

    return True, "OK"


# ---------- Bridge helpers ----------

def ensure_bridge(br_name: str, inner: str, outer: str):
    """
    Create or update a Linux bridge (using 'ip') and enslave inner/outer.
    """
    # Detach from any previous masters
    for dev in (inner, outer):
        if not dev:
            continue
        run_cmd(f"{IP} link set {dev} down")
        run_cmd(f"{IP} link set {dev} nomaster")

    # Create bridge if missing
    rc, out, err = run_cmd(f"{IP} link show {br_name}")
    if rc != 0:
        run_cmd(f"{IP} link add name {br_name} type bridge")

    # Attach ports
    for dev in (inner, outer):
        if not dev:
            continue
        run_cmd(f"{IP} link set {dev} master {br_name}")
        run_cmd(f"{IP} link set {dev} up")

    # Bring bridge up
    run_cmd(f"{IP} link set {br_name} up")


def delete_bridge(br_name: str):
    """
    Try to delete bridge if it exists.
    """
    rc, out, err = run_cmd(f"{IP} link show {br_name}")
    if rc != 0:
        return  # bridge doesn't exist
    run_cmd(f"{IP} link set {br_name} down")
    run_cmd(f"{IP} link delete {br_name} type bridge")


# ---------- Nav context ----------

@app.context_processor
def inject_nav():
    cfg = load_config()
    return {
        "nav_items": [
            {"id": "dashboard", "label": "Dashboard", "endpoint": "index"},
            {"id": "setup", "label": "Setup", "endpoint": "setup"},
        ],
        "config": cfg,
    }


# ---------- Routes ----------

@app.route("/")
def index():
    cfg = load_config()

    # Hvis vi ikke har WAN-links ennå, send til setup
    if not cfg.get("wan_links"):
        return redirect(url_for("setup"))

    mgmt = cfg.get("mgmt_interface") or guess_mgmt_interface()
    if mgmt and not cfg.get("mgmt_interface"):
        cfg["mgmt_interface"] = mgmt
        save_config(cfg)

    nic_states = []
    for link in cfg.get("wan_links", []):
        name = link.get("name", "WAN")
        inner = link.get("inner")

        if not inner:
            continue

        qdisc_info = get_qdisc_state(inner)
        nic_states.append(
            {
                "name": inner,
                "label": f"{name} (inner)",
                "qdisc": qdisc_info,
            }
        )

    return render_template(
        "index.html",
        page="dashboard",
        mgmt_interface=mgmt,
        nic_states=nic_states,
        wan_links=cfg.get("wan_links", []),
    )


@app.route("/setup", methods=["GET", "POST"])
def setup():
    cfg = load_config()

    # Sett mgmt hvis vi ikke allerede har det
    mgmt = cfg.get("mgmt_interface") or guess_mgmt_interface()
    if mgmt and not cfg.get("mgmt_interface"):
        cfg["mgmt_interface"] = mgmt
        save_config(cfg)

    if request.method == "POST":
        # Les input fra wizard – inkl. alias
        wan1_inner = request.form.get("wan1_inner") or ""
        wan1_outer = request.form.get("wan1_outer") or ""
        wan2_inner = request.form.get("wan2_inner") or ""
        wan2_outer = request.form.get("wan2_outer") or ""

        wan1_name = (request.form.get("wan1_name") or "").strip()
        wan2_name = (request.form.get("wan2_name") or "").strip()

        # Riv ned gamle bridges/qdiscs
        old_links = cfg.get("wan_links", [])
        for link in old_links:
            for dev in (link.get("inner"), link.get("outer")):
                if dev:
                    clear_qdisc(dev)
            br = link.get("bridge")
            if br:
                delete_bridge(br)

        wan_links = []

        # WAN 1
        if wan1_inner and wan1_outer:
            ensure_bridge("br-wan1", wan1_inner, wan1_outer)
            wan_links.append(
                {
                    "name": wan1_name or "WAN 1",
                    "bridge": "br-wan1",
                    "inner": wan1_inner,
                    "outer": wan1_outer,
                }
            )

        # WAN 2
        if wan2_inner and wan2_outer:
            ensure_bridge("br-wan2", wan2_inner, wan2_outer)
            wan_links.append(
                {
                    "name": wan2_name or "WAN 2",
                    "bridge": "br-wan2",
                    "inner": wan2_inner,
                    "outer": wan2_outer,
                }
            )

        cfg["wan_links"] = wan_links
        save_config(cfg)

        if wan_links:
            flash("WAN links saved and bridges created.", "success")
            return redirect(url_for("index"))
        else:
            flash("No WAN links configured – please select at least one inner/outer pair.", "info")
            return redirect(url_for("setup"))

    # GET
    setup_nics = get_setup_nics(cfg)

    return render_template(
        "setup.html",
        page="setup",
        all_nics=setup_nics,
        config=cfg,
    )


@app.route("/reset-config", methods=["POST"])
def reset_config():
    cfg = load_config()
    # Clear qdiscs and delete bridges
    for link in cfg.get("wan_links", []):
        for dev in (link.get("inner"), link.get("outer")):
            if dev:
                clear_qdisc(dev)
        br = link.get("bridge")
        if br:
            delete_bridge(br)

    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()

    flash("Configuration reset. Bridges removed and qdiscs cleared.", "info")
    return redirect(url_for("setup"))


@app.route("/configure", methods=["POST"])
def configure():
    """
    Apply netem settings to a single interface.
    Expect form fields:
      itf, delay_ms, jitter_ms, loss_pct, rate_mbit
    """
    ifname = request.form.get("itf") or request.args.get("itf")
    if not ifname:
        flash("Missing interface name.", "error")
        return redirect(url_for("index"))

    def parse_float(field: str, default: float = 0.0):
        val = request.form.get(field)
        if val is None or val == "":
            return default
        try:
            return float(val)
        except ValueError:
            return default

    delay_ms = parse_float("delay_ms", 0.0)
    jitter_ms = parse_float("jitter_ms", 0.0)
    loss_pct = parse_float("loss_pct", 0.0)
    rate_mbit = parse_float("rate_mbit", 0.0)

    ok, msg = apply_netem(ifname, delay_ms, jitter_ms, loss_pct, rate_mbit)
    if ok:
        flash(f"Applied netem on {ifname}: {msg}", "success")
    else:
        flash(f"Failed to apply netem on {ifname}: {msg}", "error")

    return redirect(url_for("index"))


@app.route("/clear", methods=["POST"])
def clear():
    ifname = request.form.get("itf") or request.args.get("itf")
    if not ifname:
        flash("Missing interface name.", "error")
        return redirect(url_for("index"))

    clear_qdisc(ifname)
    flash(f"Cleared qdisc on {ifname}", "info")
    return redirect(url_for("index"))


if __name__ == "__main__":
    # For utvikling – i prod kjører du via systemd
    app.run(host="0.0.0.0", port=8081, debug=True)
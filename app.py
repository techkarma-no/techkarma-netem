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
app.secret_key = "techkarma-netem"  # kan endres hvis du vil

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
    return proc.returncode, out.strip(), err.strip()


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
    Returns a list of all non-loopback NIC names (including bridges),
    e.g. ["ens18", "ens19", "ens20", "ens21", "ens22", "br-wan1"].
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
        if name == "lo":
            continue
        nics.append(name.strip())
    return sorted(nics)


def guess_mgmt_interface():
    """
    Guess mgmt interface as the one with an IPv4 address.
    """
    rc, out, err = run_cmd(f"{IP} -o addr show")
    if rc != 0:
        return None

    candidates = []
    for line in out.splitlines():
        # example: "2: ens18    inet 10.240.54.8/24 ..."
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            ifname = parts[1]
            ifname = ifname.split("@", 1)[0]
            if ifname != "lo":
                candidates.append(ifname)

    return candidates[0] if candidates else None


def get_setup_nics():
    """
    NICs that can be used in setup:
    - exclude mgmt
    - exclude bridges (br-*)
    - exclude loopback
    """
    cfg = load_config()
    mgmt = cfg.get("mgmt_interface") or guess_mgmt_interface()

    nics = get_all_nics()
    filtered = []

    for nic in nics:
        if nic == "lo":
            continue
        if mgmt and nic == mgmt:
            continue
        if nic.startswith("br-"):
            continue
        filtered.append(nic)

    return filtered


# ---------- Bridge management ----------


def bridge_exists(name: str) -> bool:
    rc, _, _ = run_cmd(f"{IP} link show {name}")
    return rc == 0


def delete_bridge(name: str):
    if not bridge_exists(name):
        return
    run_cmd(f"{IP} link set {name} down")
    run_cmd(f"{IP} link delete {name} type bridge")


def create_or_replace_bridge(bridge_name: str, if1: str, if2: str):
    """
    Idempotent bridge creation:
    - remove existing bridge if present
    - ensure interfaces are detached from any other master
    - attach if1/if2 to new bridge
    """
    # Remove existing bridge
    if bridge_exists(bridge_name):
        delete_bridge(bridge_name)

    # Ensure interfaces are not slaves anywhere
    for iface in (if1, if2):
        run_cmd(f"{IP} link set {iface} down")
        run_cmd(f"{IP} link set {iface} nomaster")

    # Create bridge
    run_cmd(f"{IP} link add name {bridge_name} type bridge")

    # Attach interfaces
    run_cmd(f"{IP} link set {if1} master {bridge_name}")
    run_cmd(f"{IP} link set {if2} master {bridge_name}")

    # Bring everything up
    run_cmd(f"{IP} link set {bridge_name} up")
    run_cmd(f"{IP} link set {if1} up")
    run_cmd(f"{IP} link set {if2} up")


# ---------- qdisc parsing / netem ----------


def parse_qdisc_output(raw: str):
    """
    Parse `tc qdisc show dev <if>` output into a small dict.
    We only try to extract netem parameters; otherwise we return kind only.
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

    # Use the first line only (root qdisc)
    first_line = raw.splitlines()[0]

    # Identify qdisc kind
    m_kind = re.search(r"qdisc\s+(\S+)\s+\d+:", first_line)
    if m_kind:
        kind = m_kind.group(1)
        info["parsed"]["kind"] = kind
    else:
        return info

    if info["parsed"]["kind"] != "netem":
        # we only parse details for netem
        return info

    # delay Xms
    m_delay = re.search(r"delay\s+([\d\.]+)ms", first_line)
    if m_delay:
        info["parsed"]["delay_ms"] = float(m_delay.group(1))

    # jitter Yms (optional second number in delay line)
    # e.g. "delay 100ms 20ms"
    m_delay2 = re.search(r"delay\s+([\d\.]+)ms\s+([\d\.]+)ms", first_line)
    if m_delay2:
        info["parsed"]["jitter_ms"] = float(m_delay2.group(2))

    # loss
    m_loss = re.search(r"loss\s+([\d\.]+)%", first_line)
    if m_loss:
        info["parsed"]["loss_pct"] = float(m_loss.group(1))

    # rate
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

    # Build base netem args
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

    # tbf for bandwidth if requested
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


# ---------- Global nav for sidebar ----------


@app.context_processor
def inject_nav():
    """
    Makes nav_items + current config available to all templates,
    so you can easily build a left-hand menu.
    """
    cfg = load_config()
    return {
        "nav_items": [
            {"id": "dashboard", "label": "Dashboard", "endpoint": "index"},
            {"id": "setup", "label": "Setup wizard", "endpoint": "setup"},
        ],
        "config": cfg,
    }


# ---------- Routes ----------


@app.route("/")
def index():
    cfg = load_config()

    # Hvis vi ikke har WAN-links ennå, send til wizard
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
        # outer = link.get("outer")  # vi bruker ikke outer i dashboard nå

        if inner:
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

    if request.method == "POST":
        # we auto-guess mgmt if not set
        mgmt = cfg.get("mgmt_interface") or guess_mgmt_interface()
        if mgmt:
            cfg["mgmt_interface"] = mgmt

        wan1_inner = request.form.get("wan1_inner") or ""
        wan1_outer = request.form.get("wan1_outer") or ""
        wan2_inner = request.form.get("wan2_inner") or ""
        wan2_outer = request.form.get("wan2_outer") or ""

        wan_links = []

        # WAN1
        if wan1_inner and wan1_outer:
            create_or_replace_bridge("br-wan1", wan1_inner, wan1_outer)
            wan_links.append(
                {
                    "name": "WAN 1",
                    "bridge": "br-wan1",
                    "inner": wan1_inner,
                    "outer": wan1_outer,
                }
            )

        # WAN2
        if wan2_inner and wan2_outer:
            create_or_replace_bridge("br-wan2", wan2_inner, wan2_outer)
            wan_links.append(
                {
                    "name": "WAN 2",
                    "bridge": "br-wan2",
                    "inner": wan2_inner,
                    "outer": wan2_outer,
                }
            )

        cfg["wan_links"] = wan_links
        save_config(cfg)

        if not wan_links:
            flash("No WAN links were configured. Please select at least one pair.", "info")
            return redirect(url_for("setup"))

        flash("WAN links created/updated successfully.", "success")
        return redirect(url_for("index"))

    # GET
    all_nics = get_setup_nics()

    return render_template(
        "setup.html",
        page="setup",
        all_nics=all_nics,
    )


@app.route("/reset-config", methods=["POST"])
def reset_config():
    """
    Reset configuration:
    - clear netem
    - delete bridges
    - remove config.json
    """
    cfg = load_config()

    # clear qdisc & delete bridges defined in config
    for link in cfg.get("wan_links", []):
        br = link.get("bridge")
        if br:
            clear_qdisc(br)
            delete_bridge(br)

        # rens også qdisc på inner/outer for sikkerhets skyld
        for key in ("inner", "outer"):
            iface = link.get(key)
            if iface:
                clear_qdisc(iface)

    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()

    flash("Configuration reset. Run setup again.", "info")
    return redirect(url_for("setup"))


@app.route("/configure", methods=["POST"])
def configure():
    """
    Apply netem settings to a single interface (typically ens19/ens21 osv).
    Expect form fields:
      itf, delay_ms, jitter_ms, loss_pct, rate_mbit
    """
    ifname = request.form.get("itf") or request.args.get("itf")
    if not ifname:
        flash("Missing interface name.", "error")
        return redirect(url_for("index"))

    try:
        delay_ms = float(request.form.get("delay_ms") or 0)
    except ValueError:
        delay_ms = 0.0
    try:
        jitter_ms = float(request.form.get("jitter_ms") or 0)
    except ValueError:
        jitter_ms = 0.0
    try:
        loss_pct = float(request.form.get("loss_pct") or 0)
    except ValueError:
        loss_pct = 0.0
    try:
        rate_mbit = float(request.form.get("rate_mbit") or 0)
    except ValueError:
        rate_mbit = 0.0

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
    # For utvikling – i prod kjører du via systemd / gunicorn
    app.run(host="0.0.0.0", port=8081, debug=True)

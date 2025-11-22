#!/usr/bin/env python3
import os
import json
from flask import Flask, render_template, request, redirect, url_for
from netem_core import (
    BASE_DIR,
    CONFIG_PATH,
    list_interfaces,
    load_config,
    save_config,
    ensure_bridge_for_link,
    get_qdisc_raw,
    run_cmd,
)

app = Flask(__name__)
app.secret_key = "techkarma-netem-secret"

# Sist satte netem-verdier, per inner-iface
LAST_SETTINGS = {}


def compute_health(settings):
    """
    Grov 'link health' [0-100] basert på delay/jitter/loss/rate.
    Kun for UI, ikke vitenskapelig.
    """
    if not settings:
        return 100, "good"

    delay = settings.get("delay") or 0
    jitter = settings.get("jitter") or 0
    loss = settings.get("loss") or 0
    rate = settings.get("rate")

    score = 100.0

    # Delay: opptil -25 poeng
    score -= min(delay / 3.0, 25.0)

    # Jitter: opptil -15 poeng
    score -= min(jitter / 5.0, 15.0)

    # Loss: opptil -40 poeng
    score -= min(loss * 5.0, 40.0)

    # Rate: opptil -20 poeng
    if rate is not None:
        if rate < 1:
            score -= 20.0
        elif rate < 5:
            score -= 15.0
        elif rate < 20:
            score -= 5.0

    score = max(0.0, min(100.0, score))

    if score >= 80:
        label = "good"
    elif score >= 50:
        label = "degraded"
    elif score >= 20:
        label = "bad"
    else:
        label = "dead"

    return int(round(score)), label


def get_links_from_config():
    cfg = load_config()
    if not cfg:
        return []
    return cfg.get("links", [])


def get_inner_ifaces():
    """
    Returner listen med inner-interfaces vi skal styre (ens19, ens20, ...)
    basert på config-links.
    """
    links = get_links_from_config()
    return [link["inner"] for link in links]


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """
    Førstegangs-oppsett: velg inner/outer for WAN1/WAN2, og opprett bridges.
    Dette er veldig enkelt: vi tilbyr to linker (wan1, wan2).
    Du kan bare la en av dem være tom hvis du bare vil ha én.
    """
    if request.method == "POST":
        wan1_inner = request.form.get("wan1_inner") or ""
        wan1_outer = request.form.get("wan1_outer") or ""
        wan2_inner = request.form.get("wan2_inner") or ""
        wan2_outer = request.form.get("wan2_outer") or ""

        links = []
        if wan1_inner and wan1_outer and wan1_inner != wan1_outer:
            links.append({
                "id": "wan1",
                "name": "WAN 1",
                "bridge": "br-wan1",
                "inner": wan1_inner,
                "outer": wan1_outer,
            })
        if wan2_inner and wan2_outer and wan2_inner != wan2_outer:
            links.append({
                "id": "wan2",
                "name": "WAN 2",
                "bridge": "br-wan2",
                "inner": wan2_inner,
                "outer": wan2_outer,
            })

        cfg = {"links": links}
        save_config(cfg)

        # Opprett bridges for alle links
        for link in links:
            ensure_bridge_for_link(link)

        # Re-init LAST_SETTINGS
        global LAST_SETTINGS
        LAST_SETTINGS = {link["inner"]: None for link in links}

        return redirect(url_for("index"))

    # GET
    # Hvis vi allerede har en config med linker, gå rett til index
    existing_links = get_links_from_config()
    if existing_links:
        return redirect(url_for("index"))

    # Ellers vis wizard
    try:
        ifaces = list_interfaces()
    except Exception as e:
        ifaces = []
        print("Error listing interfaces:", e)

    return render_template("setup.html", interfaces=ifaces)


@app.route("/")
def index():
    links = get_links_from_config()
    if not links:
        # Ingen config enda
        return redirect(url_for("setup"))

    inner_ifaces = [link["inner"] for link in links]

    # Sørg for at LAST_SETTINGS har entries for alle
    for iface in inner_ifaces:
        LAST_SETTINGS.setdefault(iface, None)

    statuses = {}
    for iface in inner_ifaces:
        raw = get_qdisc_raw(iface)
        s = LAST_SETTINGS.get(iface)
        health_score, health_label = compute_health(s)
        statuses[iface] = {
            "raw": raw,
            "settings": s,
            "health_score": health_score,
            "health_label": health_label,
        }

    # Re-bruker din eksisterende index.html som forventer "interfaces" og "statuses"
    return render_template("index.html", interfaces=inner_ifaces, statuses=statuses)


@app.route("/apply", methods=["POST"])
def apply():
    iface = request.form.get("iface")
    inner_ifaces = get_inner_ifaces()
    if iface not in inner_ifaces:
        return redirect(url_for("index"))

    def norm(s):
        return (s or "").strip()

    delay_s = norm(request.form.get("delay"))
    jitter_s = norm(request.form.get("jitter"))
    loss_s = norm(request.form.get("loss"))
    rate_s = norm(request.form.get("rate"))

    def to_float_or_none(s):
        try:
            v = float(s)
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None

    delay = to_float_or_none(delay_s)
    jitter = to_float_or_none(jitter_s)
    loss = to_float_or_none(loss_s)
    rate = to_float_or_none(rate_s)

    # Bygg netem-params
    netem_parts = []

    if delay is not None:
        if jitter is not None:
            netem_parts.append(f"delay {delay}ms {jitter}ms")
        else:
            netem_parts.append(f"delay {delay}ms")
    elif jitter is not None:
        netem_parts.append(f"delay 0ms {jitter}ms")

    if loss is not None:
        netem_parts.append(f"loss {loss}%")

    netem_str = ""
    if netem_parts:
        netem_str = " " + " ".join(netem_parts)

    # Rydd vekk gammel qdisc
    run_cmd(f"/usr/sbin/tc qdisc del dev {iface} root")

    cmds = []
    if rate is not None:
        # netem + tbf
        cmds.append(f"/usr/sbin/tc qdisc add dev {iface} root handle 1: netem{netem_str}")
        cmds.append(
            f"/usr/sbin/tc qdisc add dev {iface} parent 1:1 handle 10: "
            f"tbf rate {rate}mbit burst 32kbit latency 400ms"
        )
    else:
        # kun netem
        cmds.append(f"/usr/sbin/tc qdisc add dev {iface} root netem{netem_str}")

    ok = True
    for cmd in cmds:
        rc, out, err = run_cmd(cmd)
        if rc != 0:
            ok = False
            print("Error applying:", cmd, "->", err or out)
            break

    if ok:
        LAST_SETTINGS[iface] = {
            "delay": delay,
            "jitter": jitter,
            "loss": loss,
            "rate": rate,
        }

    return redirect(url_for("index"))


@app.route("/reset", methods=["POST"])
def reset():
    iface = request.form.get("iface")
    inner_ifaces = get_inner_ifaces()
    if iface not in inner_ifaces:
        return redirect(url_for("index"))

    rc, out, err = run_cmd(f"/usr/sbin/tc qdisc del dev {iface} root")
    if rc != 0 and "No such file" not in (err or ""):
        print("Error resetting qdisc:", err or out)
    LAST_SETTINGS[iface] = None

    return redirect(url_for("index"))


if __name__ == "__main__":
    # kjør på 8081 så den ikke krasjer med den gamle på 8080
    app.run(host="0.0.0.0", port=8081, debug=False)

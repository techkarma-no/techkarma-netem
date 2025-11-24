# Techkarma NetEm -- Quickstart Guide

Techkarma NetEm is a lightweight WAN emulator appliance for security,
SD-WAN and network lab environments.\
It sits transparently in-line between devices and shapes latency,
jitter, loss and bandwidth using Linux **tc/netem** and **tbf**, wrapped
in a clean web interface.

This guide explains how to deploy the appliance, connect it to your lab,
and start shaping traffic.

## 1. Deployment Options

### Option A --- OVA Appliance (recommended)

-   Prebuilt Debian VM with NetEm preinstalled\
-   Import into ESXi, Proxmox, VirtualBox, VMware Workstation, etc\
-   Web UI available immediately\
-   NICs can be added at any time

Download available through BuyMeACoffee Extras.

### Option B --- Manual Installation (open source)

``` bash
git clone https://github.com/techkarma-no/techkarma-netem
cd techkarma-netem
sudo ./install.sh
```

## 2. Network Architecture

Each WAN link uses two NICs:

    WAN1-out <--> [br-wan1] <--> WAN1-in
    WAN2-out <--> [br-wan2] <--> WAN2-in

Shaping is always applied on the **inner** NICs.

## 3. First Boot & Web UI

Access UI:

    http://<ip>:8081

Default login: - Username: techkarma\
- Password: set at first boot (OVA) or during installation

## 4. Adding and Mapping NICs

1.  Add NICs in hypervisor\
2.  Reboot/hot-attach\
3.  Open *Settings → Interfaces*\
4.  Map NICs to WAN1/WAN2

## 5. Shaping Traffic

-   **Delay**
-   **Jitter**
-   **Packet Loss**
-   **Rate (bandwidth)**

Uses `tc netem` and `tbf`.

## 6. Typical Use Cases

-   SD-WAN failover\
-   Starlink/4G/5G modelling\
-   Firewall/IPS testing

## 7. Troubleshooting

### UI not loading

    systemctl status techkarma-netem

### Traffic not passing

Check NIC mapping or bridges:

    brctl show

### Shaping ineffective

    tc qdisc show dev <nic>

## Support

Donations: https://buymeacoffee.com/techkarma\
Contact: admin@techkarma.no

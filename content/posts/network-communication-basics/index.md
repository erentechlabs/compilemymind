---
title: "Network Communication Basics: Ethernet, MAC Addressing, and How Local Networks Work"
description: "A clear, technical introduction to how local networks actually communicate — covering Ethernet frames, MAC addresses, ARP, switches, and hierarchical network design."
date: 2025-11-27
tags: ["Networking", "Cybersecurity", "Protocols", "IT", "Sysadmin"]
categories: ["technology"]
---

Networks are everywhere, but most people — including many IT professionals — treat them as black boxes. You plug in a cable, data travels, things work. But when things *don't* work, or when you're trying to understand how an attacker moves laterally inside a network, the black box has to open.

This guide covers the fundamentals of local network communication: how devices talk to each other at the Ethernet layer, how addresses work, and how the physical and logical design of a network shapes its behavior.

---

## The Modern Integrated Network

Networks were once specialized. Voice traffic ran on telephone lines. Video used dedicated satellite uplinks. Data had its own infrastructure. Today, **converged networks** carry all of this over the same physical medium — which is both elegant and a significant attack surface.

A single Ethernet cable might carry: web traffic (HTTPS), VoIP calls, video conferencing, DNS queries, management traffic (SSH, SNMP), and authentication traffic (Kerberos). Understanding what's on your network — and what *shouldn't* be — is the first step in defending it.

---

## The Four Building Blocks of a Network

| Category | Examples | Role |
|----------|----------|------|
| **End hosts** | Laptops, servers, phones | Send and receive data |
| **Shared peripherals** | Network printers, NAS | Shared resources |
| **Intermediary devices** | Switches, routers, firewalls | Direct and control traffic |
| **Transmission media** | Copper, fiber, radio | Carry the signal |

Understanding the distinction between these layers matters for both design and troubleshooting. A packet problem at Layer 1 (physical) looks very different from a routing problem at Layer 3.

---

## Physical vs. Logical Topology

Every network has two representations:

**Physical topology** — where devices actually sit and how cables run. A star topology (all cables radiating from a central switch) is the most common in modern LANs.

**Logical topology** — how data flows, regardless of physical layout. Two physically adjacent devices might communicate through a complex routed path if they're on different VLANs.

> [!TIP]
> During a network audit, discrepancies between the documented topology and the actual topology are red flags. Undocumented devices and unexpected traffic paths often hide in that gap.

---

## Ethernet: The Protocol of Local Networks

**Ethernet** (IEEE 802.3) is the dominant standard for wired LAN communication. At its heart is the **frame** — the unit of data transmission at Layer 2.

### Anatomy of an Ethernet Frame

```
┌──────────┬───────────┬──────────┬──────┬─────────┬─────┐
│ Preamble │ Dest MAC  │ Src MAC  │ Type │ Payload │ FCS │
└──────────┴───────────┴──────────┴──────┴─────────┴─────┘
```

- **Destination MAC** — who the frame is for
- **Source MAC** — who sent it
- **EtherType** — what's in the payload (`0x0800` = IPv4, `0x0806` = ARP)
- **FCS** — Frame Check Sequence for error detection

Corrupted frames (bad FCS) are silently dropped — which is why physical layer issues can cause mysterious packet loss without obvious errors.

---

## MAC Addresses: Hardware Identifiers

Every network interface has a **MAC address** — a 48-bit identifier assigned by the manufacturer.

```
00:1A:2B:3C:4D:5E
└──OUI──┘ └─Device─┘
```

The first three octets identify the manufacturer (OUI). MAC addresses are used for frame delivery within a local network segment.

**Broadcast MAC:** `FF:FF:FF:FF:FF:FF` — every device on the LAN receives this frame.

> [!WARNING]
> **MAC spoofing** — changing your MAC to impersonate another device — can bypass MAC-based access controls and enable man-in-the-middle attacks. MAC addresses are not a reliable security control.

---

## ARP: Bridging IP and MAC

When a device knows the IP address of a target but needs the MAC address to deliver a frame, it uses **ARP**:

```
1. Host A broadcasts: "Who has 192.168.1.20?"
2. Host B replies:    "That's me — my MAC is AA:BB:CC:DD:EE:FF"
3. Host A caches the mapping and uses it for future frames
```

ARP is stateless and **completely unauthenticated**. Any device can claim any IP in an ARP reply — this is the root of **ARP poisoning**.

> [!WARNING]
> **ARP poisoning** works by flooding the network with fake ARP replies, corrupting the ARP caches of target devices. When a victim believes the attacker's MAC corresponds to the gateway's IP, all traffic flows through the attacker — a perfect man-in-the-middle position. Dynamic ARP Inspection (DAI) on managed switches is the standard mitigation.

---

## Switches vs. Hubs

### Hubs (Legacy)
- Repeat incoming data to **every port** — no intelligence
- All devices share the same collision domain
- An attacker on a hub-based segment receives *all* traffic automatically

### Switches (Modern Standard)
- Forward frames **only to the correct port** using a MAC address table
- Each port is its own collision domain
- Support full-duplex operation

When a destination MAC isn't in the table yet, the switch **floods** the frame to all ports — temporarily hub-like behavior — until it learns where the destination lives.

> [!NOTE]
> **MAC flooding attacks** deliberately overflow a switch's MAC table, forcing it to flood all traffic to all ports. This lets an attacker capture traffic that would otherwise be invisible. Port security features mitigate this by limiting the number of MACs per port.

---

## Broadcast Domains and Segmentation

Every broadcast frame reaches every device on the same network segment. As a network grows, broadcast traffic accumulates and degrades performance.

**Routers** define broadcast domain boundaries — traffic between segments must pass through a router, which filters broadcasts. **VLANs** achieve similar isolation at Layer 2 on a single physical switch.

From a security perspective, segmentation is not optional. A flat network — where every device can reach every other device without restriction — is an attacker's dream.

---

## Hierarchical Network Design

Enterprise networks follow a three-layer model:

```
[Core Layer]          — High-speed backbone, minimal processing
      │
[Distribution Layer]  — Routing, policy enforcement, inter-VLAN routing
      │
[Access Layer]        — End devices connect here
```

Security controls typically live at the **distribution layer** — between where untrusted endpoints connect (access) and the high-speed core. This is where ACLs, firewall policies, and traffic inspection happen.

---

## Conclusion

Local network communication is built on layers of agreed-upon rules: Ethernet frames, MAC addressing, ARP resolution, switch-based forwarding, and broadcast domain boundaries. Every one of these mechanisms has known attack vectors. Understanding how something works is always the prerequisite for understanding how it can be broken — and more importantly, how to defend it.

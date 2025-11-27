---
title: "Internet Protocol (IP) Explained: Addressing, Subnets, DHCP, and NAT"
description: "A complete and beginner-friendly guide to IPv4, subnet masks, address types, DHCP allocation, and NAT operations."
date: 2025-11-27
tags: ["Web", "Networking", "ProblemSolving", "Programming", "Hardware"]
categories: ["technology"]
---

# Internet Protocol (IP) Explained: Addressing, Subnets, DHCP, and NAT

Every device that communicates over the Internet relies on one foundational technology: the **Internet Protocol (IP)**. Whether you are browsing the web, watching a video, or connecting to a remote server, IP ensures packets find the correct destination.

This guide summarizes the core concepts of IP addressing, subnetting, addressing types, DHCP allocation, NAT translation, and how hosts communicate across networks.

---

## What Is an IP Address?

An **IP address** is a logical identifier assigned to a device’s network interface. It ensures that:

- The device can communicate over a network  
- Packets are delivered to the correct destination  
- Responses reach the original sender

### **IPv4 Address Structure**
IPv4 addresses are **32-bit binary numbers**, split into four 8-bit groups (octets).  
Example:  
`192.168.1.5` → `11000000.10101000.00000001.00000101`

Because raw binary is difficult to read, IPv4 uses **dotted decimal notation**, where each octet ranges from **0 to 255**.

This gives IPv4 a total of **over 4 billion possible addresses**.

---

## Network & Host Portions of an IP Address

An IPv4 address is hierarchical:

- **Network portion:** Identifies the network  
- **Host portion:** Identifies the device within that network  

Example:  
`192.168.18.57` →  
- Network: `192.168.18`  
- Host: `57`

Routers only need to know **how to reach networks**, not individual hosts—making the Internet scalable.

This is similar to a telephone system: country code → area code → local number.

---

## Subnet Masks: How Devices Know the Network Boundary

An IP address alone does not tell a host which part is the network portion.  
This is determined by the **subnet mask**, also 32 bits.

Common masks:
- `/8` → `255.0.0.0`  
- `/16` → `255.255.0.0`  
- `/24` → `255.255.255.0`

A subnet mask identifies:
- `1` bits → network portion  
- `0` bits → host portion  

### **Subnetting in Action**
Example:
IP: 192.168.1.5
Mask: 255.255.255.0

yaml
Network = `192.168.1.0`  
Broadcast = `192.168.1.255`

### Calculating Hosts
If a subnet has **x host bits**, usable hosts = `2^x - 2`.

Example: `/24` → 8 host bits → `2^8 - 2 = 254` usable hosts.

---

## IP Address Classes

IPv4 addresses have 5 classes:

| Class | Range of First Octet | Default Mask | Usage |
|------|------------------------|--------------|--------|
| A | 1–126 | /8 | Very large networks |
| B | 128–191 | /16 | Medium-sized networks |
| C | 192–223 | /24 | Small networks |
| D | 224–239 | n/a | Multicast |
| E | 240–255 | n/a | Experimental |

Example classifications:
- `15.4.234.12` → Class A  
- `150.110.12.50` → Class B  
- `200.14.193.67` → Class C  

---

## Public vs. Private IP Addresses

Not all IPv4 addresses are routed on the Internet.  
**Private IP ranges** (RFC 1918) are used internally:

- A Class: **10.0.0.0/8**  
- B Class: **172.16.0.0 – 172.31.255.255**  
- C Class: **192.168.0.0 – 192.168.255.255**

Private addresses:
- Are not globally unique  
- Are not routed on the Internet  
- Provide additional security  
- Enable internal network scalability  

A special private range also exists: **127.0.0.0/8** → loopback testing (`127.0.0.1`).

---

## Unicast, Broadcast, and Multicast

Devices can communicate in three modes:

### **Unicast**
One-to-one communication.  
Example: A PC requesting a webpage from a server.

### **Broadcast**
One-to-all communication within the LAN.  
Example protocols using broadcast:
- ARP  
- DHCP Discover  

Example broadcast address for `/24` network:  
`192.168.1.255`

### **Multicast**
One-to-many, group-based communication.  
Address range: **224.0.0.0 – 239.255.255.255**

Used for:
- Live streaming  
- Online gaming sessions  
- Virtual classrooms  

Corresponding multicast MAC addresses begin with:  
`01-00-5E`

---

## How IP Addresses Are Assigned

IP addresses can be assigned:

---

### **Static Assignment**
A network admin manually enters:
- IP address  
- Subnet mask  
- Default gateway  

Used for:
- Servers  
- Printers  
- Infrastructure devices  

Advantages:
- Predictable addressing  
- More control  

Disadvantages:
- Time-consuming  
- Prone to human error  

---

### **Dynamic Assignment (DHCP)**

Most networks use **DHCP** for automatic address assignment.

DHCP provides:
- IP address  
- Subnet mask  
- Default gateway  
- DNS server  

### **DHCP Allocation Workflow**

1. **Discover** → Client broadcasts a request (`255.255.255.255`, MAC `FF:FF:FF:FF:FF:FF`)
2. **Offer** → DHCP server proposes an address  
3. **Request** → Client asks to use the offered address  
4. **ACK** → Server confirms the assignment

Clients receive an address lease, which expires unless renewed.

Home routers often act as **both DHCP server (LAN side)** and **DHCP client (WAN side)**.

---

## NAT: Network Address Translation

Since private IPs cannot be routed on the Internet, NAT converts them into public IPs.

### **Why NAT?**
- Conserves public IPv4 addresses  
- Provides security by hiding internal addresses  

## Gateways & Address Boundaries

A router connects multiple networks and defines **network boundaries**.

Each interface:
- Has its own IP address  
- Represents a separate network  

Hosts must know the router’s IP address → **default gateway**.

The gateway may be assigned:
- Statically  
- Via DHCP  

Home routers typically use:
- LAN side → private address (e.g., `192.168.1.1`)  
- WAN side → ISP-assigned public address  

---

## Conclusion

The Internet Protocol is the backbone of network communication. Understanding IPv4 addressing, subnet masks, DHCP automation, and NAT translation is essential for troubleshooting, secure network design, and effective communication between devices.

As networks grow and IPv6 adoption accelerates, these fundamentals remain crucial for anyone involved in technology, cybersecurity, or software development.

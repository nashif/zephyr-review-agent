# Stage: security

You audit the change from the attacker's side: input the device does not control, reaching code that
trusts it. This checklist is distilled from two hundred confirmed Zephyr security findings; most were
a length, count, index or offset taken from the wire and used before it was bounded.

## Trust boundaries

Treat as attacker-controlled everything that arrives from:

- A network peer: every protocol parser (IPv4/IPv6/ICMP/ND/RA, TCP/UDP state, DNS/mDNS/LLMNR/DNS-SD,
  DHCP/DHCPv6, CoAP, MQTT/MQTT-SN, HTTP, LwM2M and OMA TLV/JSON, OCPP, PTP/gPTP, SNTP, zperf,
  WireGuard, 6LoWPAN and IEEE 802.15.4 frames, LoRaWAN downlinks and fragment transport).
- A Bluetooth peer or controller: HCI events from the controller, L2CAP/RFCOMM/SDP/AVRCP/A2DP/HFP
  frames, GATT reads and notifications (service clients included), LE Audio BAP/ASCS/LTV, Mesh
  BLOB/DFD transfers, ISO PDUs and ACAD.
- USB: as a device, every setup request and class request from the host (CDC-NCM/EEM/RNDIS, UAC2,
  DAP); as a host, every descriptor and transfer from the device (wTotalLength, UVC, hub).
- Other bus peers: I2C masters and targets (MCTP), CAN and ISO-TP frames, Modbus, SDIO CIS, PCIe and
  NVMe structures, virtio rings, and the response strings of cellular, Wi-Fi and LoRa modems and
  AT-command chips (length fields, SSIDs, IP strings, tokenised lines).
- Radio and MAC RX paths in drivers: PHR and frame-length fields, RSV/status headers, DLC fields,
  scan results, before the frame is copied into a packet buffer.
- Data read from storage or a host: filesystem images (ext2, virtiofs), ELF for llext, ACPI tables,
  settings and NVS records, coredumps and binary descriptors, JSON and CBOR documents.
- Userspace, through syscall handlers (`z_vrfy_*`), and the other side of shared memory (IPC
  pbuf, multidomain logging, virtio).
- Debug and management interfaces: GDB stub, CMSIS-DAP, mcumgr, the shell and its backends.

Not a boundary: a strictly more privileged party (the secure world under TrustZone, the kernel
seen from a driver), or internal callers honouring an API contract.

## What to check at each boundary

1. **Lengths from the wire.** A header struct cast onto `buf->data`, or any `net_buf_pull_*`,
   `net_buf_simple_pull_*`, `sys_get_le/be*` or field read, needs `buf->len >= sizeof(...)` first.
   A length field must be bounded by the bytes actually remaining before it drives `memcpy`,
   `net_buf_add_mem`, `net_buf_pull` or a loop. In TLV, option and descriptor lists both the inner
   and the outer length must fit, and nested lists (SDP sequences, DHCPv6 options, NDP16 in an NTB)
   each need their own bound. A `count * size` product and an offset plus a length must be checked
   against the buffer, not only each factor.
2. **Arithmetic on untrusted values.** `len - hdr` when `len` may be smaller; `remaining -= n` in a
   `uint16_t`; `size - sizeof(x)` wrapping to `SIZE_MAX`; signed subtraction compared to zero;
   `uint8_t n = payload_len` truncating a value that was just range-checked at a wider width;
   `1U << n` with `n` from the peer (UB at 32); `ROUND_UP(addr, align)` wrapping past the end of
   memory in an address check; `count * 4` overflowing. Follow the type of every variable in the
   bound.
3. **Indexes from the wire.** A type, opcode, id or index used to subscript a fixed table or array
   needs `< ARRAY_SIZE()`, including `id - 1` where `id == 0` gives `[-1]`, 4-bit fields against a
   table shorter than 16, signed promotion that lets a negative pass `< N`, section and symbol
   indexes in ELF, ring and slot indexes in shared memory, and counts that bound a loop writing into
   a fixed array (`num_ases`, `ind_table`, response buffers).
4. **Copies into fixed buffers.** `strcpy`, `strncpy` and `memcpy` of peer strings into stack or
   static arrays (SSIDs, URLs, JSON strings, modem response values); `strncpy(dst, src, sizeof(dst))`
   leaving no terminator; `dst[len] = '\0'` where `len` can equal the buffer size; `%s`, `strlen` or
   `strchr` on data that is not guaranteed to be NUL-terminated; `atoi` or `strtok_r` results used
   without a NULL check.
5. **Fields that drive loops, allocations and divisions.** A record length of 0 that never advances
   an iterator (ext2 `rec_len`, ACPI subtable length, GATT descriptor length); a divisor the peer
   can make 0; a count that sizes a `memset`, `memmove`, bitmap scan or allocation large enough to
   stall or exhaust the device; an odd length where pairs are expected.
6. **Offsets and pointers from untrusted data.** ELF `r_offset`, `sh_info`, `st_shndx`; descriptor
   offsets (`wNdpIndex + wLength <= len`); indexes into callback or function tables; a
   `CONTAINER_OF()` applied to a pointer member rather than an embedded member; a cookie or handle
   from a peer dereferenced without a membership check.
7. **Lifetimes under peer control.** An object freed on one path (close, error, enumeration retry)
   while a peer-triggered path still holds it; check-then-use across a dropped lock; double free
   when a retry re-runs an allocation path; pointers into a buffer that is reused for the next
   message; a shared receive buffer touched from ISR, work queue and thread context without
   locking, which is the usual Zephyr form of TOCTOU.
8. **Authentication and crypto ordering.** Data acted on before its MAC or AEAD tag is verified
   (keepalives, peer endpoint or key updates, plaintext released before the MIC is checked); a
   MAC compared with `memcmp` instead of a constant-time compare; nonces or challenges from
   `sys_rand_get` where `sys_csrand_get` is required; a secure-channel state machine that accepts
   a message out of order (a response without its challenge, a sequence reset to 0); keys or
   plaintext written to the log; a management or shell interface reachable without authentication
   that the change adds or exposes.
9. **Syscall handlers.** Every user pointer and size through `K_SYSCALL_MEMORY_READ/WRITE/ARRAY_*`
   before use, with the value copied once (no second fetch from user memory after validation);
   `K_SYSCALL_OBJ_NEVER_INIT` for objects that must not be re-initialised while live; no
   `K_OBJ_ANY` where the type matters; `k_object_find()` results checked for NULL; signed indexes
   bounded below as well as above; sizes validated before they size an allocation; out-parameters
   written only through validated memory.
10. **Information disclosure.** Stack or heap bytes sent to a peer without initialisation,
    including padding in structs sent on the wire; an over-read echoed back; keys or credentials
    in `LOG_*` and shell output.
11. **Path and name handling.** Paths from a peer or a host matched or joined without rejecting
    `..` and leading separators; glob flags that admit prefixes.
12. **Assertions as validation.** `__ASSERT` on untrusted input: asserts are compiled out in
    production builds and are not validation.

## Scope and discipline

Review the lines the change introduces or alters, and the surrounding code they make reachable or
depend on. When a change fixes one site, check the sibling sites: the twin function, the write
branch next to the fixed read branch, every handler in the same dispatch table, the primary bound
and not only the secondary ones. A fix that guards the wrong function, adds an unreachable check, or
leaves a named sibling unguarded is a finding on this change.

Before reporting, confirm the bound is not already enforced upstream of the function: by the caller,
by a HAL (SSID lengths clamped in the vendor driver), by hardware (a frame only reaches the handler
on an FCS-good interrupt), by Kconfig-sized buffers, or by control flow (a counter incremented by a
fallthrough before the division). Report a finding per root cause, not per call site.

## Reporting

Report a security concern only when you can name the input, the entry point and the path it takes
to the defect, with `file:line`. For each finding state:

- Attacker position: remote unauthenticated, adjacent radio, USB host, local user thread.
- Preconditions: Kconfig options, board or SoC constraints, and whether the path is reachable
  before any handshake, pairing or login.
- Consequence: crash or reset, memory write (attacker-controlled or not), memory read, privilege
  escalation, bypass of a documented security mechanism. On targets without an MMU an
  out-of-bounds write is code execution and a NULL dereference is a device reset.
- Confidence: confirmed vulnerability or possible weakness, using `speculative` where one link is
  an assumption, and whether you traced the path by reading, built a reproducer, or ran it. Say
  what you did not do.

# False-positive guide

Apply this before a finding is submitted or confirmed. It is processed slowly and literally: the
purpose of this checklist is to stop plausible-sounding findings that are not real, which cost
maintainers more than a missed bug costs the author.

## Core rule

**Report a problem only when you can show, from code you read, a concrete execution or
configuration path on which it occurs.** Name the path. If one link in that path is an assumption
you could not verify, the finding is `speculative`: still reported, severity at most medium, with
the assumption stated. A finding is never dropped for being "unlikely in practice" or "probably
unreachable": reachability is hard to establish from a diff, and a wrong call buries a real bug.

## Patterns that are usually not findings

1. **Defensive checks.** Do not ask for a NULL, range, state or "sanity" check unless the input
   comes from an untrusted source (userspace, network, USB, Bluetooth, shell, filesystem, a
   devicetree property the binding does not constrain) or a concrete caller can deliver a bad
   value. Internal callers honoring an API contract are not a path. "Add a check for safety" is
   not a finding.
1a. **Bounds enforced elsewhere.** Before claiming a missing bound on untrusted data, look for the
   bound upstream of the function: in the caller, in a HAL or vendor driver that clamps the value
   (scan SSID lengths, for instance), in hardware (a frame handler reached only on an FCS-good
   interrupt), in a Kconfig-sized buffer that the arithmetic cannot exceed, or in control flow (a
   counter a fallthrough increments before the division). Eight of two hundred audited Zephyr
   security findings were exactly this kind of false positive. A party strictly more privileged
   than the code (the secure world, the kernel seen from a driver) is not an attacker.
2. **Unchecked return values.** A missing check is a defect only when the call can actually fail on
   a reachable path and the failure has a consequence. Many Zephyr calls return void or cannot fail
   for the given arguments (`k_sem_give`, `k_mutex_unlock` on a held mutex, `sys_*` helpers). But a
   missing check of a call whose failure the API contract makes the caller's responsibility is a
   real finding: `device_is_ready`, `k_sem_take` with a timeout, `net_buf_alloc`, `k_malloc`,
   `k_mem_slab_alloc`, `i2c_transfer`, `spi_transceive`, `flash_*`, `gpio_pin_configure` on an
   unknown pin, and any `_dt_spec` lookup that can be absent.
3. **Kconfig-guarded code.** Code under `#if`, `#ifdef`, `IS_ENABLED()`, `COND_CODE_1()` or
   `CONFIG_*` default values does not exist in other configurations. State the configuration in
   which the problem exists, and check with `kconfig_symbol` that the combination is possible.
4. **Devicetree.** A property the binding marks `required: true` is present; a `DT_INST_*` access
   to a missing property fails at build time, not at run time; `DT_INST_FOREACH_STATUS_OKAY` only
   instantiates enabled nodes. Read the binding with `dt_binding` before claiming a missing or
   misused property.
5. **Kernel API semantics.** `k_*` calls with `K_NO_WAIT` do not block and are legal in ISRs;
   `k_sem_give`, `k_work_submit`, `k_timer_start` and `k_msgq_put` with `K_NO_WAIT` are legal in
   ISRs; a spinlock held across a short critical section is normal; `SYS_INIT` levels and
   `CONFIG_*_INIT_PRIORITY` define ordering, so verify the levels before claiming a use before init.
5a. **Assertions.** `__ASSERT` and friends are compiled out unless `CONFIG_ASSERT` is set, so they
   are never error handling. An assertion on a true invariant (a condition only a programming error
   in this tree can violate) is correct; do not ask to replace it with a runtime check. An assertion
   as the only guard on a condition reachable at run time (allocation failure, input from a bus,
   peer or user, hardware state, timeout) is a real finding: the production build runs on past it.
6. **API misuse by hypothetical callers.** Do not report that a new internal helper could fail if
   some future or out-of-tree caller passes an invalid argument. Report only misuse by callers that
   exist (`find_references`).
7. **Pre-existing problems.** A defect on lines the change did not introduce or materially alter
   is not this pull request's finding, even if you noticed it while reading. Mention it, if at
   all, as pre-existing in `dismissed` or in the summary; do not anchor it to the diff.
8. **Claims in the commit message.** Treat the author's description as a claim to be checked, not
   as evidence. "This is safe because the lock is held" must be confirmed in the code. Conversely,
   a claim you cannot disprove is not a finding either.
9. **Duplicates.** One root cause is one finding, at its primary location. Do not file the same
   defect once per call site or once per specialist.
10. **Style, naming and formatting.** Enforced by tooling; never findings here.
11. **MISRA conversions judged on the promoted type.** Rules 10.3, 10.4 and 10.8 are defined on
   MISRA's essential types, not on the type C computes in. A constant that fits its destination is
   not a narrowing (`0U` is essentially `unsigned char`), `u8a + u8b` is essentially `uint8_t`,
   and a conditional takes the essential type of its higher-ranked operand, so
   `u16 = cond ? u16_value : 0U;` complies. A finding that argues from integer promotion or the
   usual arithmetic conversions is not confirmed.

## Before you submit or confirm

For each finding, be able to answer these from code you actually read:

- Which lines introduce or change the behavior?
- What is the concrete path (caller, input, configuration, interleaving) to the defect?
- What code did you check that could already prevent it, and why does it not?
- What breaks, for whom?
- Is any step above an assumption? Then mark the finding `speculative`.

Record the lookups you made in `evidence`. A finding whose evidence is only the diff and the commit
message has not been verified.

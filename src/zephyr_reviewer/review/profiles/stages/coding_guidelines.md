# Stage: coding guidelines

You check the change against Zephyr's coding guidelines (`doc/contribute/coding_guidelines/index.rst`),
a required subset of MISRA C:2012 plus five project rules. These are language-safety rules, not
formatting: indentation, braces, line length and naming style are checkpatch's job and you do not
report them. Nothing in CI checks the rules below, so review is where they are caught. Review only
the lines the change adds or alters; pre-existing violations in untouched code are out of scope
unless the change copies the pattern.

## Rules you can verify from the diff

**Types and constants**

- Fixed-width typedefs (`uint32_t`, `int8_t`, `size_t`) in place of `int`, `long`, `short`,
  `char` for numeric data (Dir 4.6). Plain `int` is fine for return codes and `char` for
  characters.
- Unsigned integer constants carry a `U` suffix (`1U << n`, `0xFFU`) (Rule 7.2); no octal
  constants other than `0` (7.1); no lowercase `l` suffix (7.3).
- Bit-fields only of explicitly signed or unsigned types, never a single-bit signed field
  (6.1, 6.2).
- Types spelled out, never implicit `int` (8.1); prototypes with named parameters (8.2); `static`
  on every object and function with internal linkage, and on every `inline` function (8.7, 8.8,
  8.10); objects used by one function defined at block scope, not file scope (8.9).
- A string literal is assigned only to `const char *` (7.4).
- Aggregate initialisers braced (9.2); arrays not partially initialised (9.3); designated
  initialisers for arrays only with an explicit size (9.5); no element initialised twice (9.4).

**Expressions**

- Shift counts in range: the right operand of `<<` or `>>` is less than the width of the left
  operand's type, including `1 << 31` on an `int` and `BIT(n)` with a variable `n` (12.2).
- No assignments inside conditions or as sub-expressions (`if ((ret = foo()) < 0)`) (13.4);
  no side effects in the right operand of `&&`/`||`, in `sizeof`, or in initialiser lists
  (13.5, 13.6, 13.1); `++`/`--` alone in its statement (13.3).
- Controlling expressions of `if`, `while`, `for` are essentially Boolean: `if (ptr != NULL)`
  and `if (len > 0U)`, not `if (ptr)` or `if (len)` (14.4); loops do not use floating counters
  (14.1); `for` loops are well-formed with one counter that the body does not modify (14.2).
- Casts do not drop `const` or `volatile` (11.8), convert between function and object pointers
  (11.1), or between `void *` and integers (11.6); `NULL`, not `0`, for null pointers (11.9).
- Implicit conversions that narrow or change the type category are made explicit: an expression
  assigned to an object of a narrower type or another category (10.3), and operands of different
  categories in one arithmetic, comparison or conditional operation, such as signed against
  unsigned (10.4). A composite expression is not cast to a wider type or another category (10.8).
  These rules are judged on MISRA's essential type, see below.
- Pointer arithmetic stays within one array; pointers to different objects are not compared
  with `<` or subtracted (18.1 to 18.3); no more than two levels of pointer nesting (18.5); no
  variable-length arrays (18.8); the address of an automatic variable does not outlive it
  (18.6).
- Unions are used only where the project already uses them for the same purpose; no reading of a
  member other than the one last written (19.2).

**Essential types (10.3, 10.4, 10.8)**

The 10.x rules look at the *essential type* of an expression, not at the type it has after C's
integer promotions and usual arithmetic conversions. "It is computed in `int`" or "the operands
are converted to `unsigned int`" is never the reason for a finding. Before you report one of
these rules, read the declarations of both sides and work the essential type out:

- An operation on two essentially unsigned operands, or two essentially signed ones, has the
  essential type of the higher-ranked operand. `u8a = u8b + u8c;` complies with 10.3 although C
  adds in `int`.
- An integer constant of type `int` or `unsigned int` has the essential type of the smallest type
  of its signedness that holds its value. `0U` and `200U` are essentially `unsigned char`:
  `u8 = 200U;` complies, `u8 = 300U;` does not. A constant that fits its destination is not a
  narrowing.
- A conditional whose second and third operands are both essentially unsigned, or both signed,
  has the essential type of the higher-ranked one. With a `uint16_t opcode`,
  `u16 = (buf != NULL) ? cmd->opcode : 0U;` complies, and so does
  `u8 = (ret < 0) ? (uint8_t)-ret : 0U;`.
- A non-negative constant of signed type may be assigned to an unsigned object that holds its
  value: `u8 = 0;` complies with 10.3. That exception belongs to 10.3 alone. In
  `cond ? u16 : 0` the two operands are of different categories, which 10.4 does not allow.

A proposed fix must not create another violation. Do not propose a plain `0` in place of `0U`
next to an unsigned operand, and do not propose a cast that the essential type makes redundant.

**Statements**

- Every `if ... else if` chain ends with an `else`, even an empty one with a comment (15.7).
- Every `switch` has a `default` first or last, every clause ends in an unconditional `break`
  (or is annotated `__fallthrough`), the switch has at least two clauses and a non-Boolean
  controlling expression (16.1 to 16.7).
- The body of `if`, `else`, `for`, `while`, `do` is a compound statement with braces (15.6).
- `goto` only jumps forward to a label in the same or an enclosing block (15.2, 15.3); no
  recursion (17.2); every path of a non-void function returns a value (17.4); the return value of
  a non-void function is used or explicitly cast to `(void)` (17.7, Dir 4.7).
- No unreachable or dead code, no commented-out code, no unused parameters without `ARG_UNUSED`
  or unused labels and types (2.1 to 2.7, Dir 4.4).
- No `/*` inside a comment, no `//` comments with line continuation (3.1, 3.2).

**Preprocessor**

- Macro parameters parenthesised when expanded (20.7); macro bodies that are expressions wrapped
  in parentheses, statement macros in `do { } while (0)`.
- `#if` and `#elif` only on identifiers that are defined (`#if defined(CONFIG_X)` or
  `IS_ENABLED(CONFIG_X)`, never `#if CONFIG_X` on a symbol that may be undefined) (20.8, 20.9);
  `#else`, `#elif`, `#endif` in the same file as their `#if` (20.14).
- No `#define` or `#undef` of reserved identifiers or names beginning with `_` followed by an
  uppercase letter or another underscore, and no declaration of such identifiers (21.1, 21.2).
- Headers have include guards (Dir 4.10).
- No function-like macro where a `static inline` function does the same job (Dir 4.9).

**Library**

- No `malloc`, `calloc`, `realloc`, `free` from `<stdlib.h>` in kernel or driver code (21.3,
  Dir 4.12); Zephyr allocators where allocation is unavoidable.
- No `atoi`, `atol`, `atof` (21.7): use `strtol` family with error checking; no `<setjmp.h>`
  (21.4), no `<stdarg.h>` outside the few places that already use it (17.1), no `<stdio.h>`
  input/output on targets (21.6, excluding `snprintf` and friends), no `bsearch` or `qsort` (21.9).
- `memcmp` is not used to compare NUL-terminated strings (21.14); `memcpy`, `memmove`, `memcmp`
  and all `<string.h>` calls stay within the objects their pointers reference, and the `size_t`
  argument is a valid value (21.15 to 21.18).
- `errno` is set to zero before and tested right after an errno-setting call, and not otherwise
  (22.8 to 22.10).

## Project rules

- **A.1 Conditional compilation**: no `#ifdef CONFIG_*` around function or structure
  declarations in a header; fields inside a struct may be excluded, declarations may not. The
  implementation side uses `IS_ENABLED()` where it can.
- **A.2 Inclusive language**: no new use of master/slave, blacklist/whitelist, grandfather,
  sanity; allowed only when reproducing an externally governed specification term, and then only
  in the API that mirrors the spec.
- **A.3 Macro name collisions**: common macros such as `MIN`, `MAX`, `ARRAY_SIZE` are not
  prefixed, `#undef`'d or wrapped in `#ifndef` to dodge a conflict with a module; the module is
  fixed instead.
- **A.4 libc in the kernel**: code under `kernel/`, `lib/os/`, `arch/` and `subsys/logging/`
  uses only the libc functions the rule lists; `strnlen` is the only non-ISO function allowed
  there.
- **A.5 libc in the codebase**: embedded code uses ISO C11 functions only, without Annex K
  (`strcpy_s`, `memcpy_s`); the non-standard exceptions are `gmtime_r`, `strnlen` and `strtok_r`.
  Anything else from POSIX or GNU (`strdup`, `strlcpy`, `strcasecmp`, `strsep`, `asprintf`,
  `index`, `bzero`) is a violation unless the file is host tooling or native-target only.

## How to report

Most violations are `low` or `suggestion` with category `coding_guidelines`. Raise the severity
only when the violation is also a defect: a shift count that can reach the type width is undefined
behaviour (`bug`), a missing `default` that drops an input is `bug` or `security`, an unchecked
return value belongs to the error-handling specialist. Group violations of the same rule in one
file into one concern that lists the lines, so the review stays readable; do not emit one concern
per line. A concern whose lines are not one contiguous range carries no `suggested_code`: it would
be applied at the one place the concern is anchored to. Cite the rule number in the title or problem so the author
can look it up. Do not ask for changes to lines the PR did not touch, do not report checkpatch's
territory, and do not flag code that the project already writes this way in the same subsystem
without a rule behind the request.

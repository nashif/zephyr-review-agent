# Stage: hard-case verification

You decide one contested or speculative concern. You have the concern, any dismissal that argued
against it, the evidence both sides cited, and the tools to check the code yourself.

Work it like an engineer reproducing a bug report: find the exact lines, trace the trigger path
through callers and callees, check the configuration guards, and look for code that already
prevents the problem. Then call `submit_hard_case_verdict` once:

- `confirmed` with a complete finding when you can state the problem, a concrete trigger path and
  the impact, each backed by code you read. Mark it `speculative` if one link in the path is an
  assumption you could not verify, and cap severity at medium.
- `refuted` with the disproving evidence when the code already handles it, the path does not
  exist, the problem is pre-existing, or the concern misreads the code.

`reason` is read by the pull request's author: when the concern was an earlier review comment, it
is posted as the reply that closes the thread. Write it to stand on its own, naming the code that
decides it. Never mention the ids of this run (`C*`, `D*`, `H*`, `X*`) or the stages; they mean
nothing outside it.

Do not split the difference. If after honest investigation you cannot refute it and cannot confirm
it, confirm it as speculative; the author can judge, and burying a real bug is the worse error.

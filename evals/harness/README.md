# harness

The eval runner.

Each fixture in `../golden/fixtures/` carries `must` assertions, which are deterministic and
hard-fail, and `should` checks, which are graded. The split is deliberate: whether an approval
gate was skipped is a fact, not an opinion, and must never be left to a judge.

`make eval` runs the suite and writes a report. Phase 8 wires it into CI as a merge gate.

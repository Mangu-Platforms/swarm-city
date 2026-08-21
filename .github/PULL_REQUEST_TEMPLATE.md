## What this changes

<!-- The defect or the goal, then what the change does. -->

## Why it is correct

<!-- The reasoning a reviewer needs. For a bug fix: what the failure was, and
     what now prevents it. -->

## Verification

<!-- What you ran and what it showed. `make verify` output, new test names,
     manual steps for anything the suite cannot cover. -->

- [ ] `make verify` passes
- [ ] New or changed behavior has a test that fails without this change
- [ ] No release gate, patch validation, or Git safety check was weakened
- [ ] Docs updated if operator-facing behavior or configuration changed

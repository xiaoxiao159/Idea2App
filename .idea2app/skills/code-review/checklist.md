# Review checklist

Re-check every finding against each item:

- [ ] Inputs: what if it's None / empty / wrong type / hostile?
- [ ] Boundaries: off-by-one, empty collection, first/last element.
- [ ] Errors: swallowed exceptions, error paths that leave state half-mutated.
- [ ] State: mutation order — is old state read before being overwritten?
- [ ] Resources: files/handles closed on every path, including errors?
- [ ] Idempotency: what happens when this runs twice?
- [ ] Names: does the name promise what the code actually does?
- [ ] Dead paths: branches that can never run (and the lie they tell).

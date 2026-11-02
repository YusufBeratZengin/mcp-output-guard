## What this changes

<!-- One paragraph. Link the issue it closes. -->

Closes #

## Why

## How it was tested

- [ ] `bash scripts/check.sh` passes locally
- [ ] New behaviour has a test that fails without the change
- [ ] No new runtime dependency
- [ ] The byte-for-byte path is untouched (unmodified responses are still forwarded as-is)
- [ ] Any new truncation path writes a note *and* a `_meta` entry

## Checklist

- [ ] Docs updated (README / README.tr.md / CHANGELOG)
- [ ] Public functions and CLI flags have type hints and help text
- [ ] I did not include real secrets or customer data in tests or examples

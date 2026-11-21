## What and why

<!-- What does this change and what problem does it solve? Link an issue if there is one (Fixes #123). -->

## How it was tested

<!-- The commands you ran and the inputs you tried. For a converter change, include an example input (a compose snippet or a template) and the output. -->

## Checklist

- [ ] `make lint test` passes (coverage stays at or above 95%)
- [ ] `make validate-k8s` passes, if anything in `k8s/` changed
- [ ] New behaviour has tests, including the error cases
- [ ] The docs are updated, if endpoints or conversion rules changed
- [ ] The change adds no secrets, tokens or personal data

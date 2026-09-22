# AWS packaging: pinned boto3, fixed a real deployment-order footgun

`scripts/build_assets.py` and `scripts/acceptance.py` (branch
`agent/release/aws-packaging`) now: pin boto3/botocore/transitive deps via a
committed sha256-hashed `scripts/requirements-aws.lock.txt`
(`pip install --require-hashes`); a plain `build` (e.g. from `npm test`) no
longer `rmtree`s the whole `infra/build/agent-runtime` dir, so it can never
silently strip a dependency set an earlier `package:aws` vendored;
`package:aws` itself wipes-then-reinstalls from the lock for reproducibility;
`npm run acceptance:json` now actually builds `package:aws` and inspects the
real vendored package (deps present at locked versions, entry points,
schemas, bundled service packages) plus a post-run repo-cleanliness check.
Added `npm run deploy:aws --workspace=infra` = `synth && package:aws &&
preflight && cdk deploy --all` as one safe ordered command.

**Needs your update** (owned by integration, not touched here):
- `docs/submission/aws-deployment-checklist.md:38-44` — the 2-step
  `synth`/`package:aws` sequence and the "do not run the ordinary infra build
  between package:aws and cdk deploy" warning are now obsolete; that ordering
  hazard no longer exists. Simplest replacement: `npm run deploy:aws
  --workspace=infra` (after `cdk bootstrap`, as before).
- `docs/submission/definition-of-done.md:54-55` — same two lines, same fix.
- `docs/workstreams/integration.md:60` already lists `package:aws` as one of
  several root-level commands; no change needed there unless you want to
  point at `deploy:aws` instead.

See `infra/README.md` "Exact deployment order" for the new documented
sequence.

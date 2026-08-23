# Release procedure

Releases are published only from `main` with the **Publish release** GitHub Actions
workflow. Never create a tag manually to bypass its acceptance checks.

1. Complete `docs/haos-acceptance.md` on a fresh Home Assistant OS system and attach
   redacted evidence to issue #2.
2. Resolve every failure and close the native blocking issues #2, #5, and #8 only when
   their acceptance criteria are met. Optional OAuth and Voice PE hardware follow-ups
   do not block the browser/speaker MVP.
3. Confirm the release commit is on `main`, its required CI checks are green, the App
   `version` is unused canonical SemVer, and `realtime_voice/CHANGELOG.md` has the same
   version heading.
4. In GitHub Actions, run **Publish release** on `main`. Enter the version without `v`
   and type `PUBLISH` only after reviewing the evidence.
5. The workflow independently reruns Ruff, every test, App lint, and an amd64 image
   build. It refuses an open blocker, mismatched/reused version, missing changelog, or
   non-`main` ref. Only then does it create the immutable `v<version>` tag and release.
6. Refresh the custom App Store and confirm the published version is offered. Install
   it once more, then add the release URL to issue #12 and close the gate.

If a workflow fails, do not reuse or move a tag. Fix the cause through a reviewed PR and
rerun the workflow. If the tag/release was created but post-publication installation
fails, document the failure, publish a new patch version, and use the rollback section
of the HA OS acceptance record; never replace the existing release artifact in place.

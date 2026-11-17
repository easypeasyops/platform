# Platform Radar scores

`scores.json` holds the platform's self-assessment on six measures, scored 0–5 from this
repo (not from the live cluster) against the rubric in its `meta`. It's the record the radar
charts are drawn from: the [Platform Radar](https://claude.ai/artifact/VemWi4hrxxuzhJf1L9rLuL)
and the ServiceMonitor page's Overview tab (PT-60), which reads this file from `main` every
hour: a scoring merged here shows there without a release.

- One row per measure per scoring: `{week, commit, axis, score, evidence, gaps, next}`. `week` is
  the scoring's date (America/Los_Angeles; the name stays from when it was always a Monday);
  `commit` is the `main` commit that was scored.
- Rows are only added, never edited: a scoring is six new rows, opened as a PR by the
  `platform-radar-weekly` task. Since 2026-11-16 it runs daily (Emrah; weekly again later) and
  adds rows only on a day the repo changed, so "the week before" on the radars is the scoring
  before.
- The axes, in order: Supply-chain security, Reliability and recovery, Observability, Delivery
  safety, Developer experience, Operability and automation. The epics that raise the ones below 4
  are named after them in Jira (PT-42, PT-43).

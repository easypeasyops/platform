# Platform Radar scores

`scores.json` holds the platform's weekly self-assessment on six measures, scored 0–5 from this
repo (not from the live cluster) against the rubric in its `meta`. It's the record the radar
charts are drawn from: the [Platform Radar](https://claude.ai/artifact/VemWi4hrxxuzhJf1L9rLuL)
and the ServiceMonitor page's Overview tab (PT-60), which reads this file from `main` every
hour: a week merged here shows there without a release.

- One row per measure per week: `{week, commit, axis, score, evidence, gaps, next}`. `week` is
  the Monday (America/Los_Angeles); `commit` is the `main` commit that was scored.
- Rows are only added, never edited: a new week is six new rows, opened as a PR every Monday by
  the `platform-radar-weekly` task.
- The axes, in order: Supply-chain security, Reliability and recovery, Observability, Delivery
  safety, Developer experience, Operability and automation. The epics that raise the ones below 4
  are named after them in Jira (PT-42, PT-43).

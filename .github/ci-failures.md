# CI failures

Every CI failure, its cause and its fix, newest first. Check here before diagnosing a new one.
Owner: devOps. Get the log with `gh run view <run id> --log-failed`.

| Date | PR | Check | Run | Error | Cause | Fix |
|---|---|---|---|---|---|---|
| 11-10 | #11 | gds | 42759427163 | `OpenROAD.CheckMacroInstances failed: No macro instance u_mem.u_pm.u_sram found` (and `u_fmem`), 2 min in | `project.v` renamed the SRAM wrappers to `u_col1`/`u_col2`; `src/config.json` `MACROS.instances` and `PDN_MACRO_CONNECTIONS` still used the old names | config.json uses the new names (a3c7fff). Rename instances and their config.json entries in the same commit. |

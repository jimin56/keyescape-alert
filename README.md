# nudge

A small cron that checks a few things I care about and pushes to my phone with [ntfy](https://ntfy.sh) when something changes.

- `check.py` — fetch adapters + state diffing. Targets live in `config.json` (not committed); `config.example.json` shows the shape.
- `.github/workflows/check.yml` — schedules. `state.json` is what was open at the last pass.
- `mac/` — optional local runner (launchd, every minute) that shares `state.json` with CI through git.

```
python3 check.py --status
```

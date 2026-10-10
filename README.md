# Codebase Dashboard

Point it at a folder and get stats in your browser: lines of code by language, the
largest files, and (when the folder is a git repository) commit activity and churn
over time. Each scan is remembered, so later scans show what changed.

Standard library Python only, nothing to install. Runs locally on
http://127.0.0.1:8765 and never sends anything anywhere.

## Use it

```
dashboard.cmd                       start it and open the page (recent folders)
dashboard.cmd C:\code\my-repo       open straight on that folder
python dashboard.py --no-browser    start the server without opening a tab
```

Paste a folder path into the search bar to scan it. Starting it again while it runs
just opens the page. Recent folders are kept in `recent.json` (up to 30) and scan
history in `history/`, both next to the script and not part of the repository.

Works well with [App Launcher](https://github.com/Laumerz84/app-launcher), which
can start and stop it with one click.

# System tips

`run_command` uses argv by default (safer). Set `shell=true` only for pipes/redirects — it needs confirmation.
Prefer typed tools (`disk_usage`, `list_processes`) over raw shell when possible.

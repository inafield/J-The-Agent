# Files tips

All paths go through SafetyGuard. Writes, mkdir, copy, move, and trash require confirmation.
Use `create_directory` for folders. Prefer `patch_file` for small edits over rewriting whole files.
`find_files` uses glob patterns (e.g. `*.py`); `path_info` shows size/mtime.

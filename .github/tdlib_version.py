"""Print the version and the commit of the installed TDLib, as stored in TDLIB_VERIFIED."""

from tgmcp.tdlib import execute

print(execute("getOption", name="version")["value"],
      execute("getOption", name="commit_hash")["value"])

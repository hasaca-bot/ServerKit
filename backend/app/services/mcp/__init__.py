"""Core MCP server: per-user, tenant-scoped tools behind POST /api/v1/mcp.

    protocol.py    JSON-RPC 2.0 / MCP methods (initialize, tools/list, tools/call, ping)
    registry.py    tool registry
    context.py     the caller and the tenant-scoped resource lookups every tool uses
    validation.py  input rules and output redaction (ported from the publish gateway)
    tools_*.py     the tools
    publish.py     publish_site (GitHub through the caller's own connection)
    jobs.py        slow actions as deployment jobs, polled with get_job
"""

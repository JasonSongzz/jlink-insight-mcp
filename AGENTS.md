# JLink Insight MCP development guide

Python 3.10+, FastMCP, pylink-square, pyelftools, intelhex. Keep upstream MIT attribution.

- Register public MCP tools in src/jlink_mcp/server.py; tool logic belongs in tools/.
- Project detection must report evidence and ambiguity; never assume source presence means runtime support.
- Use hardware_access around whole hardware transactions. Monitoring owns an exclusive lease; do not bypass it.
- Verify every firmware byte before reset/run. Refuse generic core flash fallback and ambiguous probe/artifact selection.
- Match the installed pylink API. Register access takes integer indices, RTT read returns byte values, RTT start takes a control-block address.
- Monitoring defaults to non-intrusive operation. Fault sampling must restore the prior run state; report any restoration failure.
- Keep binary RTT separate from text logs. Lost or malformed trace must reduce confidence, not fabricate execution history.
- Diagnostic findings must include evidence and observation scope. Threshold warnings do not prove a deadlock or system correctness.
- Keep work queues, raw bytes, event windows and report payloads bounded. Stop workers and release resources on shutdown.
- Tests: python -m pip install -e ".[dev]"; python -m pytest. Hardware tests are opt-in and require a selected board.
- New module/public function docstrings should explain English / 中文 intent; use type annotations.
- Do not claim hardware validation from mocks. Update docs and validation notes when behavior changes.

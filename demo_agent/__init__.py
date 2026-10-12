"""Demo support agents for Wardlock (SPEC §14) and the scripted attacker (T19).

- agent_c: an LLM support agent that refunds only through Wardlock's MCP tools (config C).
- agent_a: the same agent and prompt using PayPal's own create_refund directly (config A, the baseline).
- orchestrator: the trusted code that reads a ticket and starts a run.
- scripted_attacker: no LLM; tries every known way to move money it should not.
"""

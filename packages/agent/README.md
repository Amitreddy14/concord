# agent

The LangGraph runtime: state schema, graph topology, nodes and prompt assembly.

Phase 3 builds the vertical slice (triage, retrieve, draft). Phase 5 adds the supervisor and
specialist subgraphs. Phase 6 adds the approval gate.

Two rules hold for every node written here:

- **A node that interrupts performs no mutation before the interrupt.** Resumption re-executes
  the node from its start, so anything before the interrupt runs twice (ADR 0001).
- **Untrusted text enters a prompt only through the assembler in `prompts.py`,** which takes it
  as a separate argument from the instruction template and places it in a delimited data block
  (ADR 0005).

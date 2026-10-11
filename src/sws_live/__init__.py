"""The live composition root for SWS's first real ``ec2:StopInstances`` run.

This package sits deliberately **outside** the ``sws_agent`` package, and that
placement is a load-bearing part of the safety argument rather than an accident
of layout.

``sws_agent`` is the hermetic core. ADR 0009 confines SDK imports to exactly two
modules inside it (``ec2_mutation_client`` and ``mutation_evidence``), and M15-E's
boundary gate (``tests/test_m15e_mutation_client.py::
test_no_composition_wires_the_mutation_factory``) statically proves that *no*
module inside the package acquires a mutation client. Keeping the one component
that legitimately acquires that client out of the package means that gate stays
exactly as strict as it was written -- it is never weakened to make room for the
live path. The live root imports ``sws_agent`` as a library; the library never
imports the live root.

What lives here:

* :mod:`sws_live.live_stop` -- the composition builder (real mutation client +
  real durable stores + real observer + the approval-signature gate) and the two
  orchestration steps, ``prepare`` and ``execute``.
* :mod:`sws_live.cli` -- the fixed command-line entry point the SSM invocation
  document calls.

Nothing here is imported by ``sws_agent``, the MCP server, or the simulator.
``STOP_RESOURCE.implemented`` remains ``False``; the live path is enabled by
*which handler is injected*, never by a registry flag, so
``preflight.check_program_invariants`` (which refuses if any action is marked
implemented) is satisfied rather than broken.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.2.0"

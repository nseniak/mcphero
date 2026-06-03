"""Parameterized contract suite for ``SandboxService``.

Every behavior the abstraction promises is asserted here against each
registered backend. As each backend implementation lands, the
corresponding entry in :func:`backends.iter_backends` flips from
``skip`` to a real instance and the contract suite starts asserting
parity coverage automatically.
"""

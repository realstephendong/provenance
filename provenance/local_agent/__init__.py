"""The private data plane: one person, one device, one encrypted store.

Nothing in this package talks to the shared Provenance service, and nothing in the
shared service imports from it. That is the boundary, expressed as a dependency
rule rather than a promise in a comment.
"""

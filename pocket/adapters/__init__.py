"""Private backend adapters; pairing and authorization remain in the host."""

from .openclaw import OpenClaw
from .pi import Pi


def create_runner(config, state):
    backend = config.get("backend", "openclaw")
    if backend == "openclaw":
        return OpenClaw(config, state)
    if backend == "pi":
        return Pi(config, state)
    raise ValueError("Unsupported backend")

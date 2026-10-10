"""Provider-reported input/output tokens, never a text-length estimate."""

import math


class Reply(str):
    def __new__(cls, text, envelope):
        obj = super().__new__(cls, text)
        inner = envelope.get("result", envelope)
        usage = inner.get("usage") or inner.get("meta", {}).get("agentMeta", {}).get("usage") or {}
        usage = usage if isinstance(usage, dict) else {}

        def count(key):
            value = usage.get(key)
            return value if type(value) is int and 0 <= value < 10**12 else None

        obj.output_tokens = count("output")
        obj.cache_read_tokens = count("cacheRead")
        obj.cache_write_tokens = count("cacheWrite")
        # OpenClaw normalizes input separately from cacheRead/cacheWrite.
        uncached = count("input")
        obj.input_tokens = (
            (uncached + (obj.cache_read_tokens or 0) + (obj.cache_write_tokens or 0))
            if uncached is not None
            else None
        )
        return obj


def measured(reply, elapsed):
    if not math.isfinite(elapsed) or elapsed <= 0:
        return None
    return {
        "output_tokens": getattr(reply, "output_tokens", None),
        "input_tokens": getattr(reply, "input_tokens", None),
        "cache_read_tokens": getattr(reply, "cache_read_tokens", None),
        "cache_write_tokens": getattr(reply, "cache_write_tokens", None),
        "elapsed_seconds": elapsed,
        "basis": "turn_wall_time",
    }

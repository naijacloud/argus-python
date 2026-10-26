"""The one place the version lives.

The wheel's metadata (hatch reads this file), the envelope's `sdk.version`, the
OTLP `telemetry.sdk.version` and the User-Agent all come from here, so they can
never disagree.
"""

__version__ = "0.1.0"

#: The wire contract this SDK implements (nc-argus spec, section 10).
CONTRACT = "argus.sdk/1"

#: `sdk.name` on envelopes and `telemetry.sdk.name` on OTLP (spec section 4).
SDK_NAME = "argus.python"

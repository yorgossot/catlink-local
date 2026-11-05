"""catlink-local: run a CATLINK Open X litter box without the vendor cloud.

Two small stdlib-only services:
  * authresp -- the HTTPS bootstrap responder the box asks for its MQTT broker address;
  * gateway  -- the box-facing MQTT broker plus the Home Assistant MQTT-discovery bridge.
"""

__version__ = "0.1.0"

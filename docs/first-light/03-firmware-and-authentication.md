# Firmware and authentication

Firmware 7.18 and newer asks the JSON-RPC port (4700) for a key-signed handshake. Astronomo does not perform it. The same firmware runs an **ASCOM Alpaca** server on port 32323 that needs no key. How to connect that way is in the README ("Connect a real Seestar"). What the 2026-11-13 night left open is in the [open hardware questions](05-first-light-checklist.md).

## What the JSON-RPC port requires

Astronomo's default transport talks to the Seestar the way community projects do: JSON-RPC over TCP port 4700. The community documentation says that behaviour depends on the **firmware version**:

| Firmware | What the community reports | What Astronomo does (JSON-RPC transport) |
|---|---|---|
| Older than about 2583 | No client authentication | Sends plain commands |
| About 2583 to 2704 | Commands need a `"verify": true` parameter | Detects the firmware from `get_device_state` and adds it only in that range |
| **7.18 and newer** (`version_int` 2718 and up) | A mandatory **RSA challenge-response handshake** (`get_verify_str`, sign, `verify_client`, `pi_is_verified`) before any command is accepted | **Not implemented, on purpose.** Connecting fails with a message that names this note and the Alpaca transport |
| Newer still (9.75 / app 3.4.0 reported) | The handshake reply changed shape | Not handled |

Sources: the `seestar_alp` project (merged change adding client authentication for 7.18+) and the `seestarpy` documentation, <https://seestarpy.readthedocs.io/en/latest/info/authentication.html>. None of this is from ZWO. The ranges are from community notes. This S50 Pro on firmware 9.75 was driven over Alpaca, so the older JSON-RPC ranges were not seen on it.

## The Alpaca server is the firmware's own

The Seestar firmware runs an ASCOM Alpaca HTTP server on port 32323. That is separate from `seestar_alp`, a standalone controller that speaks the JSON-RPC ports and is not the firmware's Alpaca server. Community notes (an occultation report, a Cloudy Nights technical reference, and the ARGOS notes for an S30 Pro on firmware 7.18) describe that server as part of the firmware and as needing no key. On 2026-11-13 an S50 Pro on firmware 9.75 connected the same way. The capture calls are in `tools/seestar_alpaca.py`.

## Why the handshake is not implemented

The community's documentation says the signing key is the same for every device and is **extracted from ZWO's own Android app**. The projects that use it describe it as interoperability under US law (17 U.S.C. 1201(f)) and say the legality varies by country and that you are responsible for compliance. That is a legal and ethical question about **your** device, **your** country and **ZWO's terms**, not a technical one, so it is your decision and not something to add quietly. The Alpaca route exists so that you do not have to make that decision. If you choose to go the other way, do it with a community tool that is built for it, after reading its notice, and tell the project if you want the driver to support a user-supplied key file.

# Security and safety

Astronomo can drive a real telescope and prepares files for outside services, so "security" here covers two things:
**physical safety** (what the software can make the telescope do) and **ordinary security** (secrets, data, the dashboard).

A Seestar S50 Pro has finished supervised visits (2026-11-18, Hat-P-23, firmware 9.75, Alpaca). Read
[Real hardware](docs/real-hardware.md) first;
pointing a telescope at or near the Sun can destroy its optics and sensor and cause eye injury.

## Report privately

Use GitHub's **private vulnerability report**: on the repository page, open the **Security** tab and choose
**Report a vulnerability**. Only the maintainer sees it. If that button is not there, open a *Bug report* that says only
"I have a safety or security report and need a private way to send it", with no details, and the maintainer will arrange one.

Please report privately, not in a public issue:

* **A safety interlock that did not hold.** The weather, Sun, Moon, window or altitude rules in `core/interlocks.py` let a
  mission observe when they should have refused or stopped.
* **A command that could move the telescope when it should not**, or that could point it at or near the Sun.
* **A way to make Astronomo send something to a person or a service without a human reviewing it.** Nothing is meant to be sent by the software.
* **A leaked secret**: an API key, an observer or station code that should not be public, or a path or address that identifies someone.
* **Anything that lets another machine on the network control the telescope or the dashboard.**

Say what you ran, the version (the commit hash), what you expected and what happened. Do not include your location,
observer name, codes or network addresses unless they are needed to reproduce the problem.

## What to expect

This is a small, volunteer-run project. The maintainer will try to acknowledge a report within a week and to say what
will be done. There is no bug bounty, and no formal support timeline.

## Not a security report

Wrong numbers, a parser that fails on a real file, a threshold that needs re-setting, or a driver call the telescope does
not understand are ordinary bugs: use the [issue templates](.github/ISSUE_TEMPLATE/). Unconfirmed behaviour on real
hardware is expected and is listed in the [open hardware questions](docs/hardware-questions.md).

## Things this project does on purpose

* It does **not** implement the client-authentication handshake that newer Seestar firmware needs, and it does not accept
  contributions of extracted vendor keys. See [the firmware note](docs/notes/firmware-and-authentication.md).
* The dashboard is a **local, read-mostly** tool and has no login. Do not expose it to the internet.
* `.env` is git-ignored and is where API keys and codes belong; never commit it.

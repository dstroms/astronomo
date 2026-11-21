# Contributing to Astronomo

Astronomo's mission is to **help amateur astronomers contribute meaningfully to real science**. It is a small project
with a narrow job, so the most useful contributions are the ones that make a night's data more trustworthy or a first
night easier. Thank you for helping.

**Status:** a Seestar S50 Pro has finished two supervised variable-star visits (Hat-P-23, 2026-11-18, 12 frames each).
The magnitudes match Gaia G. The live AAVSO file was held because the telescope Wi-Fi has no internet. A clean
40-frame night and a known transit have not been run. The most valuable thing anyone can do right now is a setup
where the computer reaches the internet and the telescope at the same time, and an honest report of a night
(see [Run it on a real telescope](#run-it-on-a-real-telescope) and [Connect a real Seestar](docs/connect-a-seestar.md)).

## The ground rules

These are the project's promises, and a change that breaks one will not be merged however useful it is.

1. **Nothing is sent by the software.** Astronomo prepares upload files and a person reviews and submits them. Do not add code that emails, uploads or posts on a user's behalf. (A reviewed, human-approved send path is a roadmap item, not something to add quietly.)
2. **Honest numbers.** A quoted error has to be true, and a number has to say what it measures. No claim beyond the data: no discovery claims, and a routine night is not dressed up as a result.
3. **Say what is unconfirmed.** Anything that depends on how a real Seestar behaves and has not been seen on one must be labelled as unconfirmed in the code comment and the docs. Do not present a guess as a finding.
4. **Safety stays in code.** Weather, Sun, Moon, window and altitude interlocks live in `core/` and cannot be bypassed by a plugin or a model. A change that weakens one needs a reason in the pull request, and a test.
5. **The core is domain-agnostic.** `core/` and `tools/` know nothing about exoplanets or asteroids; a science case is a plugin. If you find yourself adding a planet or an asteroid to `core/`, it belongs in a plugin.
6. **Tests never touch the network or a telescope.** The suite blocks network fetches by default (see `tests/conftest.py`). A test that needs list or catalogue content patches the fetch itself, or uses a small fixture from `tests/data/`.
7. **Credit and licence third-party data.** If a change uses someone's data, say where it came from and under what terms. If the terms are unstated, ask the owner before you add it, and do not copy the files into the repository.
8. **Vendor keys and key extraction are not accepted.** Newer Seestar firmware needs a client-authentication handshake that Astronomo deliberately does not implement (see [the firmware note](docs/notes/firmware-and-authentication.md)). Do not contribute extracted vendor keys or code that extracts them. If you think a supported route exists (an official API, Alpaca), open an issue first.

## Getting set up

Python 3.10 or newer (a pull request runs 3.10; the daily run covers 3.10, 3.11, 3.12 and 3.13). No hardware, API key or cost is needed. The [README quick start](README.md#quick-start) is `python3 install.py`. The commands below are that install without questions, plus the test suite.

```bash
cd astronomo
python3 install.py --non-interactive --no-self-test   # venv, install, and a .env; asks nothing
source .venv/bin/activate

python main.py --simulate --fast       # a full mission in about 10 s
pytest -n auto                         # the whole suite on every core, about 4 to 5 minutes
pytest -n auto -m "not slow"           # skips the tests that run a whole simulated mission; about 4 to 5 minutes too on a laptop
cd frontend && npm test                # React render smoke (Node 18+; not part of pytest)
```

The same install by hand is `python3 -m venv .venv`, then `pip install -r requirements.txt`. Those are the packages in `pyproject.toml`.

A few tests skip without a real Ollama server. That is expected. GitHub Actions runs the quick check on a pull request and on a push to `main` that leaves Python, `requirements.txt`, `pytest.ini`, and the workflow file alone. A push that changes one of those, a manual run, and a daily run at 06:00 Mountain Standard Time run the whole suite (`.github/workflows/tests.yml`). The same workflow always runs `cd frontend && npm test` so a broken React render is visible without putting Node in the Python install. If you change a non-dashboard module,
restart `python -m dashboard.api`; the UI process does not reload them.

Before you open a pull request, run the whole pytest suite, not just your own tests. If you changed `frontend/`, run `cd frontend && npm test` too.

## Ways to help

### Run it on a real telescope

This is the most valuable contribution, and the one with the most risk, so read [Real hardware](docs/real-hardware.md),
the [first-clean-night runbook](docs/runbooks/first-clean-night.md), and the [open hardware questions](docs/hardware-questions.md) first. Supervise every run, and never point a telescope at or near
the Sun. Then open an issue with:

* your firmware version and the app version;
* the output of `python main.py --hardware-check` (the saved report in `data/hardware_checks/`);
* which rows of the open hardware questions your telescope answered;
* what broke, and what the telescope actually replied (not what you expected).

Remove your location, observer name, any codes and any network address you do not want public before you post.

### Share what a night found

An upload file that the AAVSO or the MPC accepted or rejected is useful evidence. Open an issue with the file's
header (not your observer code if you prefer), the destination's message, and the Astronomo version. A real frame set
that breaks the photometry is just as useful: tell us the exposure, the target and what went wrong.

### Fix or improve the code

* **Bug fixes:** include a test that fails without the fix.
* **A new science case:** write a plugin. Start with [Writing a plugin](docs/writing-a-plugin.md); a plugin lives in `plugins/<name>/` and implements the small interface in `core/base_plugin.py`. Say which network wants it and what file it produces. Each plugin ships with a page in `docs/plugins/` that states what its numbers do and do not mean.
* **A new network or file format:** build it from the destination's published specification, link that specification in the code, and test it against a trimmed **real** sample where one can be obtained. Do not ship a parser you only tested on samples you wrote yourself and call it verified. Say which it is.
* **Thresholds:** the focus, pointing and rotation limits are guesses made before a telescope arrived. A change to one should come with measurements from real frames.
* **Documentation:** wrong, stale or unclear docs are bugs. The docs say what is verified and what is not; please keep that line honest.

### Report a problem

* **A safety or security problem** (an interlock that did not hold, a command that could move the telescope when it should not, a leaked secret): report it privately, as described in [SECURITY.md](SECURITY.md). Do not post details that would help someone point a telescope at the Sun.
* **A run on a real telescope:** open a *First-light report* issue. **A file a network accepted or rejected:** open an *Upload file result* issue.
* **A change that is not on the roadmap:** open a *Feature request*. Read the [roadmap](ROADMAP.md) first; if the change is already listed, open a pull request instead. Describe the observing problem (a night that cannot be reduced, a file a network wants, or a first-light step that is missing). The ground rules above still apply.
* **Something that does not work as documented:** open a *Bug report* with what you ran, what you expected and what happened.

## Pull requests

* One change per pull request, with a short description of **why**.
* Say what you ran: the full suite, and `python main.py --simulate --fast` for anything that touches a mission.
* Say what is verified and what is not. "Tested only in simulation" is a perfectly good thing to write.
* Update the docs and the [roadmap](ROADMAP.md) if your change moves something from "not real yet" to "built", and keep the numbers in the README (test count) current.
* **A new document needs a row in the roadmap's [documentation map](ROADMAP.md#documentation-map).** The roadmap is the master document and `tests/test_docs_linked.py` fails on an unlisted document or a broken link.
* Match the surrounding code. Comments say why, not what.

## Licence

Astronomo is released under the [Apache License 2.0](LICENSE). By submitting a contribution you agree that it is
licensed under the same terms (Section 5 of the licence). Do not submit code or data you do not have the right to share.

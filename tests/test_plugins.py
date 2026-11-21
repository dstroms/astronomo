from dataclasses import replace

import pytest

import main
from core.base_plugin import BaseExperimentPlugin, PluginContractError
from plugins import PluginLoadError, available_plugins, draft_refusal, load_plugin, plugin_status
from plugins.asteroid_finder.plugin import AsteroidFinderPlugin


def test_base_class_is_abstract():
    with pytest.raises(TypeError):
        BaseExperimentPlugin(None)  # type: ignore[abstract]


def test_incomplete_plugin_cannot_be_instantiated(settings):
    class Partial(BaseExperimentPlugin):
        name = "partial"

        def generate_targets(self):
            return []

    with pytest.raises(TypeError, match="abstract"):
        Partial(settings)


def test_registry_loads_active_plugin_by_name(settings):
    assert "asteroid_finder" in available_plugins()
    plugin = load_plugin(settings.active_plugin, settings)
    assert plugin.name == settings.active_plugin == "variable_stars"  # the default is a plugin that is ready
    assert plugin.status == "ready"
    assert isinstance(load_plugin("asteroid_finder", settings), AsteroidFinderPlugin)


def test_registry_rejects_unknown_plugin(settings):
    with pytest.raises(PluginLoadError, match="Unknown plugin"):
        load_plugin("does_not_exist", settings)


def test_asteroid_plugin_honours_the_contract(settings):
    plugin = load_plugin("asteroid_finder", settings)
    targets = plugin.validated_targets()
    assert len(targets) == 3 and {"name", "ra_deg", "dec_deg"} <= targets[0].keys()
    plan = plugin.validated_exposure_plan()
    assert {"gain", "exposure_s", "filter", "count"} <= plan.keys() and plan["count"] >= 3


def _plugin(settings, targets=None, plan=None):
    class P(AsteroidFinderPlugin):
        def generate_targets(self):
            return targets

        def get_exposure_plan(self):
            return plan

    return P(settings)


@pytest.mark.parametrize(
    "targets,msg",
    [
        ("nope", "list"),
        ([{"name": "a", "ra_deg": 1.0}], "dec_deg"),
        ([{"name": "a", "ra_deg": 1.0, "dec_deg": 95.0}], "invalid coordinates"),
        ([{"name": "a", "ra_deg": float("nan"), "dec_deg": 1.0}], "invalid coordinates"),
        ([{"name": "a", "ra_deg": 1, "dec_deg": 1}, {"name": "a", "ra_deg": 2, "dec_deg": 2}], "duplicate"),
    ],
)
def test_target_validation(settings, targets, msg):
    with pytest.raises(PluginContractError, match=msg):
        _plugin(settings, targets).validated_targets()


def test_target_ra_is_normalised(settings):
    out = _plugin(settings, [{"name": "a", "ra_deg": -10.0, "dec_deg": 5.0}]).validated_targets()
    assert out[0]["ra_deg"] == pytest.approx(350.0)


@pytest.mark.parametrize(
    "plan",
    [{"gain": 1, "exposure_s": 10, "filter": "x"}, {"gain": 1, "exposure_s": 0, "filter": "x", "count": 1},
     {"gain": 1, "exposure_s": 10, "filter": "x", "count": 0}],
)
def test_exposure_plan_validation(settings, plan):
    with pytest.raises(PluginContractError):
        _plugin(settings, plan=plan).validated_exposure_plan()


# --------------------------------------------------------------------------- #
# Draft plugins: kept, tested, and not allowed to command a real telescope
# --------------------------------------------------------------------------- #
DRAFTS = {"asteroid_finder", "rubin_followup"}


def test_only_the_unproven_plugins_are_drafts():
    status = {name: plugin_status(name)[0] for name in available_plugins()}
    assert {n for n, st in status.items() if st == "draft"} == DRAFTS
    assert status["variable_stars"] == status["transit_finder"] == "ready"
    assert all(plugin_status(n)[1] for n in DRAFTS), "a draft must say why"


def test_a_plugin_with_an_unknown_status_does_not_load(settings, monkeypatch):
    monkeypatch.setattr(AsteroidFinderPlugin, "status", "beta")
    with pytest.raises(PluginLoadError, match="status"):
        load_plugin("asteroid_finder", settings)


@pytest.mark.parametrize("name", sorted(DRAFTS))
def test_a_draft_is_refused_on_a_real_telescope_unless_allowed(settings, name):
    real = replace(settings, mock_mode=False)
    plugin = load_plugin(name, real)
    why = draft_refusal(plugin, real)
    assert why and name in why and "not ready for use" in why and "ASTRONOMO_ALLOW_DRAFT_PLUGINS" in why
    assert draft_refusal(plugin, replace(real, allow_draft_plugins=True)) is None
    assert draft_refusal(load_plugin(name, settings), settings) is None  # simulation is fine
    assert draft_refusal(plugin, replace(real, data_source="archive")) is None  # an archive replay moves no hardware


def test_a_ready_plugin_is_never_refused(settings):
    real = replace(settings, mock_mode=False)
    for name in ("variable_stars", "transit_finder"):
        assert draft_refusal(load_plugin(name, real), real) is None


def test_main_refuses_a_draft_on_a_real_telescope(settings, monkeypatch, capsys):
    real = replace(settings, mock_mode=False, site_source="file", active_plugin="asteroid_finder")
    monkeypatch.setattr(main, "build_settings", lambda args: real)
    assert main.main(["--plugin", "asteroid_finder"]) == main.EXIT_FAILED
    err = capsys.readouterr().err
    assert "is a draft and not ready for use" in err and "will not command a real telescope" in err


def test_list_plugins_marks_the_drafts(capsys):
    assert main.main(["--list-plugins"]) == main.EXIT_OK
    lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()}
    assert "draft" not in lines["variable_stars"] and "(default)" in lines["variable_stars"]
    assert "draft" not in lines["transit_finder"]
    assert all("[draft: not ready for use" in lines[n] for n in DRAFTS)


@pytest.mark.parametrize("name", sorted(DRAFTS))
def test_the_dashboard_header_says_draft(name):
    from dashboard.plugin_ui import load_plugin_ui

    assert load_plugin_ui(name).headline().startswith("DRAFT, not ready for use")
    assert not load_plugin_ui("variable_stars").headline().startswith("DRAFT")

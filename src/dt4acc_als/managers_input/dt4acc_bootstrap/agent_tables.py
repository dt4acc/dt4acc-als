"""Use dt4acc_agent's generated magnet tables in place of the native ones.

The native bootstrap stays in charge of everything but the magnets: BPMs,
turn by turn data, master clock / RF, twiss, track, tune. For the magnet
power supplies the liaison entries and conversions come from the tables
dt4acc_agent generated for ALS (``resources/agent_generated``), which agree
with ALS's MML on all 368 supplies (worst 0.027 %): quadrupoles use the
exact ramp-table excitation curves instead of the native energy-scaled
multiplier, and the skew quadrupoles (SQSF, SQSD) are driven too.

Device ids follow the native bootstrap: a setpoint is
``(<Setpoint channel>, set_current)``, a readback ``(<Monitor channel>,
read_current)``. The agent keys readbacks on the setpoint channel
(``rdbk_current``); they are renamed here using the AO channel names.

Select it with ``DT4ACC_ALS_MAGNET_TABLES=agent`` (see run_als_twin).
"""

from __future__ import annotations

import logging
from importlib.resources import files
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple, Union

import jsons
import yaml

from dt4acc.core.model.view import Monitor, Setpoint
from dt4acc_lib.bl.liaison_manager import LiaisonManager
from dt4acc_lib.bl.translator_service import (
    TranslatorService,
    create_translation_object_for_data_model,
)
from dt4acc_lib.bl.unit_conversion import EnergyIndependentCurveUnitConversion, calculate_brho
from dt4acc_lib.bl.yellow_pages import YellowPages
from dt4acc_lib.interfaces.utils.state_conversion import StateConversion
from dt4acc_lib.model.utils.command import ReadCommand
from dt4acc_lib.model.utils.identifiers import ConversionID, DevicePropertyID, LatticeElementPropertyID
from dt4acc_lib.model.utils.liaison_manager_lookup_table import (
    LiaisonManagerForwardLookupElement,
    LiaisonManagerForwardLookupTable,
    LiaisonManagerInverseLookupElement,
    LiaisonManagerInverseLookupTable,
)
from dt4acc_lib.model.utils.translator_manager_lookup_table import CurvePoint, TranslatorLookupTable

from .config import als_bootstrap_config
from .liaison_builder import create_liaison_lut
from .read_lattice import als_get_lattice, default_energy
from .translator_builder import create_translator_luts
from .yellow_pages_builder import create_yellow_pages_input
from ..readin_ao import als_ring_ao_data, load_ramp_data

logger = logging.getLogger("dt4acc")

#: lattice properties the magnet supplies write; native entries on them are replaced
MAGNET_LATTICE_PROPERTIES = {"main_strength", "x_kick", "y_kick"}
#: supply families taken from the agent tables. BEND and RF stay native
#: (the twin does not drive BEND, RF is the master clock); VCMCHICANE is
#: left out as MML itself fails on it, so it could not be checked.
AGENT_FAMILIES = {"QF", "QD", "QFA", "QDA", "SF", "SD", "SQSF", "SQSD", "HCM", "VCM", "HCMCHICANE"}

_PREFIX = "als_"
_RESOURCES = ("managers_input", "resources", "agent_generated")


def _load_yaml(name: str):
    path = files("dt4acc_als").joinpath(*_RESOURCES, _PREFIX + name)
    with path.open("rt") as fp:
        return yaml.load(fp, yaml.CLoader)


def _load_json(name: str):
    import json

    with files("dt4acc_als").joinpath(*_RESOURCES, name).open("rt") as fp:
        return json.load(fp)


class LayeredTranslatorService(TranslatorService):
    """Agent conversions first, then the native translator service."""

    def __init__(self, *, conversions: Mapping[ConversionID, StateConversion], native: TranslatorService):
        super().__init__(lut=native.lut, brho=native.brho)
        self.conversions = dict(conversions)
        self.native = native

    def register_reference_cache(self, cache):
        self.native.register_reference_cache(cache)

    def get(self, id_: ConversionID) -> Union[StateConversion, None]:
        r = self.conversions.get(id_)
        if r is not None:
            return r
        return self.native.get(id_)


def setpoint_to_monitor_channels(ao_model) -> Dict[str, str]:
    """Setpoint channel -> monitor channel, from the AO of all families."""
    r: Dict[str, str] = {}
    for family_name in ao_model.keys():
        fam = ao_model[family_name]
        try:
            sp, mon = fam.Setpoint.ChannelNames, fam.Monitor.ChannelNames
        except AttributeError:
            continue
        if isinstance(sp, str):
            sp, mon = [sp], [mon]
        for s, m in zip(sp, mon):
            s, m = str(s).strip(), str(m).strip()
            if s and m:
                assert r.get(s, m) == m, f"{s} has monitors {r[s]} and {m}"
                r[s] = m
    return r


def _agent_supplies() -> Dict[str, str]:
    """Setpoint channel -> family, for the supplies taken from the agent."""
    by_pc = _load_json("magnets_by_pc.json")
    return {pc: magnets[0]["family"] for pc, magnets in by_pc.items() if magnets[0]["family"] in AGENT_FAMILIES}


def _rename(dev: DevicePropertyID, supplies, set_to_mon) -> Union[DevicePropertyID, None]:
    """Agent device id -> twin device id; None for ids the twin does not use."""
    if dev.device_name not in supplies:
        return None
    if dev.property == "set_current":
        return dev
    if dev.property == "rdbk_current":
        return DevicePropertyID(device_name=set_to_mon[dev.device_name], property="read_current")
    return None


def agent_magnet_tables(ao_model, brho: float):
    """Agent liaison entries and conversions, renamed to the twin's device ids.

    Returns (forward entries, inverse entries, conversions, {set channel: monitor channel}).
    """
    supplies = _agent_supplies()
    set_to_mon = setpoint_to_monitor_channels(ao_model)
    missing = sorted(pc for pc in supplies if pc not in set_to_mon)
    assert not missing, f"no monitor channel in the AO for {missing}"

    inv = jsons.load(_load_yaml("liaison_manager_inverse_lookup_table.yml"), LiaisonManagerInverseLookupTable)
    fwd = jsons.load(_load_yaml("liaison_manager_forward_lookup_table.yml"), LiaisonManagerForwardLookupTable)

    inverse = []
    for e in inv.lut:
        dev = _rename(e.dev_id, supplies, set_to_mon)
        if dev is not None:
            inverse.append(LiaisonManagerInverseLookupElement(dev_id=dev, lat_ids=list(e.lat_ids)))

    forward = []
    for e in fwd.lut:
        devs = [_rename(d, supplies, set_to_mon) for d in e.dev_ids]
        # the twin's forward entries point at the readback (monitor) only
        devs = [DevicePropertyID(device_name=set_to_mon[d.device_name], property="read_current")
                for d in devs if d is not None and d.property == "set_current"]
        if devs:
            forward.append(LiaisonManagerForwardLookupElement(lat_id=e.lat_id, dev_ids=devs))

    conversions: Dict[ConversionID, StateConversion] = {}

    def add(cid_dev: DevicePropertyID, lat: LatticeElementPropertyID, make):
        dev = _rename(cid_dev, supplies, set_to_mon)
        if dev is not None:
            conversions[ConversionID(lattice_property_id=lat, device_property_id=dev)] = make()

    ts = jsons.load(_load_yaml("translation_service_lookup_table.yml"), TranslatorLookupTable)
    for e in ts.lut:
        cid = e.conversion_id
        add(cid.device_property_id, cid.lattice_property_id,
            lambda info=e.conversion_info: create_translation_object_for_data_model(info, brho))

    for e in _load_yaml("translation_service_curves.yml")["lut"]:
        cid = e["conversion_id"]
        points = [CurvePoint(indep=i, dep=d) for i, d in e["points"]]
        add(DevicePropertyID(**cid["device_property_id"]), LatticeElementPropertyID(**cid["lattice_property_id"]),
            lambda points=points, e=e: EnergyIndependentCurveUnitConversion(
                fwd_points=points, bwd_points=points, brho=brho,
                flip_dep_sign=e["flip_dep_sign"], length=e["length"],
            ))

    pairs = {pc: set_to_mon[pc] for pc in supplies}
    return forward, inverse, conversions, pairs


def _without_native_magnets(entries: Iterable, lat_ids_of) -> List:
    return [e for e in entries if not any(l.property in MAGNET_LATTICE_PROPERTIES for l in lat_ids_of(e))]


def load_managers(lat=None):
    """As bootstrap.load_managers, with the magnet supplies from the agent tables."""
    if lat is None:
        lat = als_get_lattice()
    ao_model = als_ring_ao_data()
    ramp_data = load_ramp_data(ao_model)
    config = als_bootstrap_config()
    brho = calculate_brho(default_energy)

    yp = YellowPages(create_yellow_pages_input(ao_model, lat, config))
    fwd_lut, inv_lut, process_variable_views, mappings = create_liaison_lut(yp, ao_model, lat)
    native_lm = LiaisonManager(forward_lut=fwd_lut, inverse_lut=inv_lut)
    ts_lut = create_translator_luts(yp, native_lm, ao_model, ramp_data, lat, mappings=mappings, reference_energy=default_energy)
    native_ts = TranslatorService(lut=ts_lut, brho=brho)

    a_fwd, a_inv, conversions, pairs = agent_magnet_tables(ao_model, brho)

    forward = _without_native_magnets(fwd_lut.lut, lambda e: [e.lat_id]) + a_fwd
    inverse = _without_native_magnets(inv_lut.lut, lambda e: e.lat_ids) + a_inv
    fwd = LiaisonManagerForwardLookupTable(forward)
    inv = LiaisonManagerInverseLookupTable(inverse)
    fwd.verify()
    inv.verify()
    assert not inv.non_unique_entries()
    lm = LiaisonManager(forward_lut=fwd, inverse_lut=inv)
    ts = LayeredTranslatorService(conversions=conversions, native=native_ts)

    process_variable_views = list(process_variable_views) + _missing_pv_views(process_variable_views, pairs)
    logger.info(
        "agent magnet tables: %d supplies, %d conversions, %d forward / %d inverse liaison entries",
        len(pairs), len(conversions), len(a_fwd), len(a_inv),
    )
    return yp, lm, ts, process_variable_views


def _missing_pv_views(views: Sequence[Union[Monitor, Setpoint]], pairs: Mapping[str, str]) -> List[Union[Monitor, Setpoint]]:
    """Setpoint/monitor PV pairs for agent supplies the native bootstrap has no PVs for (the skews)."""
    known = {v.pv_name for v in views}
    r: List[Union[Monitor, Setpoint]] = []
    for set_pv, mon_pv in sorted(pairs.items()):
        if set_pv in known:
            continue
        mon = Monitor(pv_name=mon_pv, rcmd=ReadCommand(mon_pv, "read_current"), prec=3, record_type="ai", treat_returned_data="average")
        setp = Setpoint(pv_name=set_pv, rcmd=ReadCommand(set_pv, "set_current"), prec=3, record_type="ao",
                        reads=[mon.rcmd], treat_returned_data="average")
        r += [mon, setp]
    return r

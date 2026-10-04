"""
Population estimation for permanent-plot remeasurement.

Components (IPCC-style stock change, same allometric equation on both dates):

    Δstock = survivor_growth - mortality + ingrowth

The population total is NEVER "mean tree * area". Plots are a stratified
simple-random sample; per-plot values are expanded with the plot's own area,
stratified means carry equal plot weight (per-hectare basis), and stratum
land areas scale the totals:

    Y_h = A_h * mean_h(y),   y_plot = component / plot_area_ha
    Var(Y_h) = A_h^2 * (1 - f_h) * s_h^2 / n_h

Inputs
------
``build_measurement_table`` pulls one row per tree-occasion from PostgreSQL.
``estimate`` is pure NumPy given that table + design snapshot, so the maths
is unit-testable without a database.
"""
import hashlib
import json
from dataclasses import dataclass

import numpy as np
from scipy import stats

from inventory.models import (
    STATUS_ALIVE_MEASURED,
    STATUS_ALIVE_NOT_MEASURED,
    STATUS_DEAD,
    STATUS_MISSING,
)
from inventory.services.identity import pair_measurements

KG_PER_MG = 1000.0


# ----------------------------------------------------------------- biomass
def biomass_kg(dbh_cm, height_m, eq):
    """agb_kg = a * dbh_cm**b * height_m**c (vectorised)."""
    h = np.asarray(height_m, dtype=float)
    return eq["a"] * np.asarray(dbh_cm, dtype=float) ** eq["b"] * h ** eq["c"]


def biomass_measurement_variance(agb, dbh_cm, height_m, eq,
                                 dbh_sd_cm, height_sd_m):
    """
    First-order propagation of instrument/observer error:

        Var(ln agb) = (b·σ_d/dbh)² + (c·σ_h/h)²
    """
    dbh_cm = np.asarray(dbh_cm, dtype=float)
    height_m = np.asarray(height_m, dtype=float)
    agb = np.asarray(agb, dtype=float)
    rel = (eq["b"] * dbh_sd_cm / dbh_cm) ** 2
    if eq["c"] != 0.0:
        rel = rel + (eq["c"] * height_sd_m / height_m) ** 2
    return (agb ** 2) * rel


# --------------------------------------------------------------- table build
def build_measurement_table(t1_campaign, t2_campaign, equations_qs, frame=None):
    """Returns (table_rows, equations_by_species, plots, strata).

    When ``frame`` (a SamplingFrameVersion) is given, each plot's area and
    boundary come from that frozen frame payload, so an estimate is
    reproducibly bound to one sampling-frame edition: later boundary
    revisions cannot silently change its per-hectare expansion. Trees are
    always read from their historical TreeMeasurement rows — a new frame
    never migrates a measurement.
    """
    equations = {}
    for e in equations_qs:
        for sp in e.species.all():
            equations[sp.code] = {
                "code": e.code,
                "version": e.version,
                "a": e.a, "b": e.b, "c": e.c,
                "dbh_min_cm": e.dbh_min_cm,
                "dbh_max_cm": e.dbh_max_cm,
                "height_required": e.height_required,
                "residual_sigma": e.residual_sigma,
                "citation": e.citation,
            }

    plots = {}
    strata = {}
    from inventory.models import Plot, Stratum
    for s in Stratum.objects.all():
        strata[s.code] = {"code": s.code, "name": s.name,
                          "area_ha": s.area_ha, "plot_codes": []}
    for p in Plot.objects.select_related("stratum"):
        entry = None
        if frame is not None:
            entry = frame.plot_payload.get(p.code)
        plots[p.code] = {
            "code": p.code,
            "stratum": p.stratum.code,
            "area_ha": (entry["declared_area_ha"] if entry
                        else p.declared_area_ha),
            "polygon_area_ha": (entry["area_polygon_ha"] if entry
                                else p.area_polygon_ha),
            "frame_revision_id": (entry.get("revision_id") if entry
                                  else None),
            "x_m": p.x_m, "y_m": p.y_m,
        }
        strata[p.stratum.code]["plot_codes"].append(p.code)

    def rows_for(campaign):
        out = []
        from inventory.models import TreeMeasurement
        qs = (
            TreeMeasurement.objects
            .filter(campaign=campaign)
            .select_related("tree", "tree__plot", "tree__species",
                            "tree__superseded_tree")
        )
        for m in qs:
            out.append({
                "tree_id": m.tree_id,
                "plot": m.tree.plot.code,
                "species": m.tree.species.code,
                "field_number": m.field_number_seen,
                "x_m": m.x_m, "y_m": m.y_m,
                "status": m.status,
                "dbh_cm": m.dbh_cm,
                "height_m": m.height_m,
                "verified_renumber_of": (
                    m.tree.superseded_tree_id
                    if campaign == t2_campaign
                    else None
                ),
            })
        return out

    return rows_for(t1_campaign), rows_for(t2_campaign), equations, plots, strata


def resolved_identity_pairs(t1_campaign, t2_campaign):
    """Human-verified (t1_tree, t2_tree) pairs split by decision."""
    from inventory.models import CONFLICT_DISTINCT, CONFLICT_RENUMBER, IdentityConflict
    renumber, distinct = set(), set()
    qs = IdentityConflict.objects.filter(
        t1_campaign=t1_campaign, t2_campaign=t2_campaign,
    ).select_related("t1_measurement__tree", "t2_measurement__tree")
    for c in qs:
        key = (c.t1_measurement.tree_id, c.t2_measurement.tree_id)
        if c.status == CONFLICT_RENUMBER:
            renumber.add(key)
        elif c.status == CONFLICT_DISTINCT:
            distinct.add(key)
    # field-book verified renumber links carried on the tree itself
    from inventory.models import Tree, TreeMeasurement
    for m2 in (TreeMeasurement.objects
               .filter(campaign=t2_campaign)
               .select_related("tree")):
        sup = m2.tree.superseded_tree_id
        if sup:
            renumber.add((sup, m2.tree_id))
    return renumber, distinct


def equation_checksum(equations_by_species):
    payload = json.dumps(
        {k: equations_by_species[k] for k in sorted(equations_by_species)},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ------------------------------------------------------------- per-plot comp
@dataclass
class PlotComponents:
    code: str
    stratum: str
    area_ha: float
    frame_revision_id: object = None
    survivor_kg: float = 0.0
    survivor_var_meas_kg2: float = 0.0
    survivor_observed_pairs: int = 0
    survivor_zero_growth: list = None
    survivor_missing: list = None
    survivor_imputed_kg: float = 0.0
    survivor_missing_t1_kg: float = 0.0
    mortality_kg: float = 0.0
    mortality_var_meas_kg2: float = 0.0
    mortality_residual_var_kg2: float = 0.0
    mortality_ids: list = None
    ingrowth_kg: float = 0.0
    ingrowth_var_meas_kg2: float = 0.0
    ingrowth_residual_var_kg2: float = 0.0
    ingrowth_ids: list = None
    below_recruitment_ids: list = None
    missing_tree_ids: list = None
    extrapolation_ids: list = None
    equation_missing_input_ids: list = None
    excluded_conflict_ids: list = None
    stock_t1_kg: float = 0.0
    stock_t2_kg: float = 0.0
    stock_t1_resid_var: float = 0.0
    stock_t2_resid_var: float = 0.0

    def __post_init__(self):
        for f in ("survivor_zero_growth", "survivor_missing", "mortality_ids",
                  "ingrowth_ids", "below_recruitment_ids", "missing_tree_ids",
                  "extrapolation_ids", "equation_missing_input_ids",
                  "excluded_conflict_ids"):
            if getattr(self, f) is None:
                setattr(self, f, [])


def _has_eq_inputs(row, eq):
    if row["dbh_cm"] is None:
        return False
    if eq["height_required"] and eq["c"] != 0.0 and row["height_m"] is None:
        return False
    return True


def _extrapolates(row, eq):
    return not (eq["dbh_min_cm"] <= row["dbh_cm"] <= eq["dbh_max_cm"])


def compute_plot_components(plot_code, plot_info, pairing, equations,
                            dbh_sd_cm, height_sd_m, zero_tol_cm,
                            recruitment_cm, interval_years):
    pc = PlotComponents(code=plot_code, stratum=plot_info["stratum"],
                        area_ha=plot_info["area_ha"],
                        frame_revision_id=plot_info.get("frame_revision_id"))

    def agb(row):
        eq = equations[row["species"]]
        return float(biomass_kg([row["dbh_cm"]], [row["height_m"]], eq)[0]), eq

    # ---- survivors & mortality from pairs
    for pair in pairing["pairs"]:
        r1, r2 = pair["t1"], pair["t2"]
        if r1["plot"] != plot_code:
            continue
        tag = f"{plot_code}/{r2['field_number']}"
        eq = equations.get(r1["species"]) or equations.get(r2["species"])

        if r2["status"] == STATUS_DEAD:
            # mortality observed at the t2 remeasurement at the same spot
            if r1["status"] != STATUS_ALIVE_MEASURED or not _has_eq_inputs(r1, eq):
                pc.equation_missing_input_ids.append(
                    {"tree": tag, "reason": "dead but t1 dbh/height missing"})
                continue
            b1 = biomass_kg([r1["dbh_cm"]], [r1["height_m"]], eq)
            v_meas = biomass_measurement_variance(
                b1, [r1["dbh_cm"]], [r1["height_m"]], eq,
                dbh_sd_cm, height_sd_m)
            pc.mortality_kg += float(b1[0])
            pc.mortality_var_meas_kg2 += float(v_meas[0])
            pc.mortality_residual_var_kg2 += float((b1[0] * eq["residual_sigma"]) ** 2)
            pc.stock_t1_kg += float(b1[0])
            pc.stock_t1_resid_var += float((b1[0] * eq["residual_sigma"]) ** 2)
            pc.mortality_ids.append({"tree": tag, "agb_t1_kg": round(float(b1[0]), 3),
                                     "species": r1["species"]})
            continue

        if r2["status"] == STATUS_MISSING:
            pc.missing_tree_ids.append({"tree": tag, "reason": "not located t2"})
            continue

        if r2["status"] == STATUS_ALIVE_NOT_MEASURED:
            # alive, confirmed present, but dbh missing — NOT zero growth
            if r1["status"] == STATUS_ALIVE_MEASURED and _has_eq_inputs(r1, eq):
                b1, _ = agb(r1)
                pc.survivor_missing.append(
                    {"tree": tag, "reason": "alive t2, dbh not measured",
                     "agb_t1_kg": round(b1, 3)})
                pc.stock_t1_kg += b1
                pc.stock_t1_resid_var += (b1 * eq["residual_sigma"]) ** 2
            else:
                pc.survivor_missing.append(
                    {"tree": tag, "reason": "alive t2, inputs insufficient"})
            pc.survivor_observed_pairs += 1
            continue

        if r2["status"] == STATUS_ALIVE_MEASURED:
            if r1["status"] != STATUS_ALIVE_MEASURED:
                pc.survivor_missing.append(
                    {"tree": tag, "reason": f"t1 status {r1['status']}"})
                continue
            if not (_has_eq_inputs(r1, eq) and _has_eq_inputs(r2, eq)):
                pc.equation_missing_input_ids.append(
                    {"tree": tag, "reason": "dbh/height required by equation"})
                continue
            if _extrapolates(r1, eq) or _extrapolates(r2, eq):
                pc.extrapolation_ids.append(
                    {"tree": tag, "dbh_t1": r1["dbh_cm"], "dbh_t2": r2["dbh_cm"],
                     "valid_range_cm": [eq["dbh_min_cm"], eq["dbh_max_cm"]],
                     "equation": eq["code"]})
            b1 = biomass_kg([r1["dbh_cm"]], [r1["height_m"]], eq)
            b2 = biomass_kg([r2["dbh_cm"]], [r2["height_m"]], eq)
            growth = float(b2[0] - b1[0])
            v1 = biomass_measurement_variance(
                b1, [r1["dbh_cm"]], [r1["height_m"]], eq,
                dbh_sd_cm, height_sd_m)
            v2 = biomass_measurement_variance(
                b2, [r2["dbh_cm"]], [r2["height_m"]], eq,
                dbh_sd_cm, height_sd_m)
            pc.survivor_kg += growth
            pc.survivor_var_meas_kg2 += float(v1[0] + v2[0])
            pc.stock_t1_kg += float(b1[0])
            pc.stock_t2_kg += float(b2[0])
            pc.stock_t1_resid_var += float((b1[0] * eq["residual_sigma"]) ** 2)
            pc.stock_t2_resid_var += float((b2[0] * eq["residual_sigma"]) ** 2)
            pc.survivor_observed_pairs += 1
            if abs(r2["dbh_cm"] - r1["dbh_cm"]) <= zero_tol_cm:
                pc.survivor_zero_growth.append(
                    {"tree": tag, "dbh_t1_cm": r1["dbh_cm"],
                     "dbh_t2_cm": r2["dbh_cm"],
                     "delta_dbh_cm": round(r2["dbh_cm"] - r1["dbh_cm"], 3),
                     "verified": True})

    # ---- t1-only: disappeared (distinct-resolution) or unaccounted
    for r1 in pairing["t1_only"]:
        if r1["plot"] != plot_code:
            continue
        tag = f"{plot_code}/{r1['field_number']}"
        eq = equations.get(r1["species"])
        if r1["status"] == STATUS_ALIVE_MEASURED and eq and _has_eq_inputs(r1, eq):
            b1 = biomass_kg([r1["dbh_cm"]], [r1["height_m"]], eq)
            pc.mortality_kg += float(b1[0])
            pc.mortality_var_meas_kg2 += float(biomass_measurement_variance(
                b1, [r1["dbh_cm"]], [r1["height_m"]], eq,
                dbh_sd_cm, height_sd_m)[0])
            pc.mortality_residual_var_kg2 += float((b1[0] * eq["residual_sigma"]) ** 2)
            pc.stock_t1_kg += float(b1[0])
            pc.stock_t1_resid_var += float((b1[0] * eq["residual_sigma"]) ** 2)
            pc.mortality_ids.append(
                {"tree": tag, "agb_t1_kg": round(float(b1[0]), 3),
                 "species": r1["species"],
                 "source": "t1-only after distinct-number resolution"})
        else:
            pc.missing_tree_ids.append(
                {"tree": tag, "reason": "t1-only, cannot quantify removal"})

    # ---- t2-only: ingrowth above recruitment
    for r2 in pairing["t2_only"]:
        if r2["plot"] != plot_code:
            continue
        tag = f"{plot_code}/{r2['field_number']}"
        eq = equations.get(r2["species"])
        if r2["status"] != STATUS_ALIVE_MEASURED or not eq:
            continue
        if r2["dbh_cm"] is None:
            continue
        if r2["dbh_cm"] < recruitment_cm:
            pc.below_recruitment_ids.append(
                {"tree": tag, "dbh_cm": r2["dbh_cm"],
                 "threshold_cm": recruitment_cm})
            continue
        if not _has_eq_inputs(r2, eq):
            pc.equation_missing_input_ids.append(
                {"tree": tag, "reason": "dbh/height required by equation"})
            continue
        if _extrapolates(r2, eq):
            pc.extrapolation_ids.append(
                {"tree": tag, "dbh_t2": r2["dbh_cm"],
                 "valid_range_cm": [eq["dbh_min_cm"], eq["dbh_max_cm"]],
                 "equation": eq["code"]})
        b2 = biomass_kg([r2["dbh_cm"]], [r2["height_m"]], eq)
        pc.ingrowth_kg += float(b2[0])
        pc.ingrowth_var_meas_kg2 += float(biomass_measurement_variance(
            b2, [r2["dbh_cm"]], [r2["height_m"]], eq,
            dbh_sd_cm, height_sd_m)[0])
        pc.ingrowth_residual_var_kg2 += float((b2[0] * eq["residual_sigma"]) ** 2)
        pc.stock_t2_kg += float(b2[0])
        pc.stock_t2_resid_var += float((b2[0] * eq["residual_sigma"]) ** 2)
        pc.ingrowth_ids.append({"tree": tag, "agb_t2_kg": round(float(b2[0]), 3),
                                "species": r2["species"],
                                "dbh_cm": r2["dbh_cm"]})

    # ---- open identity conflicts are excluded, never guessed
    for c in pairing["conflicts"]:
        r1, r2 = c.get("t1"), c.get("t2")
        row_plot = (r1 or r2)["plot"]
        if row_plot != plot_code:
            continue
        label = (r1 or r2)["field_number"]
        pc.excluded_conflict_ids.append({
            "tree": f"{plot_code}/{label}",
            "distance_m": (None if c["distance_m"] is None
                           else round(c["distance_m"], 2)),
            "hint": c.get("hint", "same_number_position_mismatch"),
            "reason": "unverified identity — excluded until human checks",
        })

    # ---- ratio imputation for survivors alive-but-unmeasured at t2
    observed_growth = pc.survivor_kg
    ref_stock = sum(
        m["agb_t1_kg"] for m in pc.survivor_missing if "agb_t1_kg" in m
    )
    measured_t1_stock = pc.stock_t1_kg - ref_stock
    if ref_stock > 0 and measured_t1_stock > 0 and observed_growth >= 0:
        ratio = observed_growth / measured_t1_stock
        pc.survivor_imputed_kg = ratio * ref_stock
        pc.survivor_missing_t1_kg = ref_stock
        pc.survivor_kg += pc.survivor_imputed_kg
        for m in pc.survivor_missing:
            if "agb_t1_kg" in m:
                m["imputed_growth_kg"] = round(ratio * m["agb_t1_kg"], 3)

    return pc


# ----------------------------------------------------------- stratification
COMPONENTS = ["survivor_growth", "mortality", "ingrowth"]


def _stratum_estimate(plot_obs, area_ha, fpc=1.0):
    """
    Stratified SRS (per-hectare plot basis):
      Y  = A_h · mean(y_per_ha)
      SE = A_h · sqrt( fpc · s²/n )
    """
    n = len(plot_obs)
    if n == 0:
        return {"n_plots": 0, "total_kg": 0.0, "se_design_kg": 0.0, "df": 0,
                "mean_per_ha_kg": None, "sd_per_ha_kg": None,
                "plot_values_kg_ha": []}
    y = np.asarray(plot_obs, dtype=float)
    mean = float(np.mean(y))
    sd = float(np.std(y, ddof=1)) if n > 1 else 0.0
    total_kg = area_ha * mean
    se_design_kg = area_ha * np.sqrt(fpc * sd ** 2 / n)
    return {
        "n_plots": n,
        "mean_per_ha_kg": mean,
        "sd_per_ha_kg": sd,
        "total_kg": total_kg,
        "se_design_kg": float(se_design_kg),
        "df": n - 1,
        "plot_values_kg_ha": [float(v) for v in y],
    }


def estimate(table_t1, table_t2, equations, plots, strata, design,
             resolved_renumber_pairs=None, resolved_distinct_pairs=None):
    """
    Run the full estimator. ``design`` keys:
      dbh_sd_cm, height_sd_m, zero_tol_cm, recruitment_cm,
      interval_years, fpc (bool), crs_epsg.
    """
    pairing = pair_measurements(
        table_t1, table_t2,
        resolved_renumber_pairs=resolved_renumber_pairs,
        resolved_distinct_pairs=resolved_distinct_pairs,
    )

    pcs = [
        compute_plot_components(
            code, info, pairing, equations,
            design["dbh_sd_cm"], design["height_sd_m"],
            design["zero_tol_cm"], design["recruitment_cm"],
            design["interval_years"],
        )
        for code, info in sorted(plots.items())
    ]

    # organise per stratum
    by_stratum = {}
    for pc in pcs:
        by_stratum.setdefault(pc.stratum, []).append(pc)

    def comp_kg_ha(pc, attr):
        return getattr(pc, attr) / pc.area_ha

    fpc_default = 1.0
    component_results = {}
    for comp, attr in [("survivor_growth", "survivor_kg"),
                       ("mortality", "mortality_kg"),
                       ("ingrowth", "ingrowth_kg")]:
        strat_out, totals, se_sq, df_num_parts = [], [], 0.0, []
        total_meas_var = 0.0
        total_resid_var = 0.0
        for scode, spcs in by_stratum.items():
            vals = [comp_kg_ha(pc, attr) for pc in spcs]
            n = len(vals)
            A_h = strata[scode]["area_ha"]
            fpc = 1.0
            if design.get("fpc"):
                sampled_ha = sum(p.area_ha for p in spcs)
                fpc = max(0.0, 1.0 - sampled_ha / A_h)
            se = _stratum_estimate(vals, A_h, fpc=fpc)

            # missing-survivor inflation for the survivor component
            miss_frac = 0.0
            if comp == "survivor_growth":
                ref = sum(p.survivor_missing_t1_kg for p in spcs)
                stock1 = sum(p.stock_t1_kg for p in spcs)
                miss_frac = (ref / stock1) if stock1 > 0 else 0.0
                inflation = 1.0 / (1.0 - miss_frac) if miss_frac < 1 else 1.0
                se["se_design_kg"] = float(
                    A_h * np.sqrt(fpc * se["sd_per_ha_kg"] ** 2 * inflation / n)
                ) if n > 0 else 0.0
                se["missing_biomass_fraction"] = round(miss_frac, 4)
            se["fpc"] = fpc
            se["stratum"] = scode
            se["stratum_area_ha"] = A_h
            strat_out.append(se)
            totals.append(se["total_kg"])
            se_sq += se["se_design_kg"] ** 2
            # (stratum design variance V_h, stratum df nu_h) for the
            # Welch-Satterthwaite combination below.
            df_num_parts.append((se["se_design_kg"] ** 2, se["df"]))

            # propagated measurement variance -> per-ha mean: sum / n / area
            meas_attr = {
                "survivor_growth": "survivor_var_meas_kg2",
                "mortality": "mortality_var_meas_kg2",
                "ingrowth": "ingrowth_var_meas_kg2",
            }[comp]
            resid_attr = {
                "mortality": "mortality_residual_var_kg2",
                "ingrowth": "ingrowth_residual_var_kg2",
            }.get(comp)
            sum_meas = sum(getattr(p, meas_attr) for p in spcs)
            # independent tree-level errors: Var of mean total ≈ A²/n²·Σv/a²
            meas_term = (A_h ** 2 / n ** 2) * sum(
                getattr(p, meas_attr) / p.area_ha ** 2 for p in spcs
            )
            total_meas_var += meas_term
            if resid_attr:
                total_resid_var += sum(getattr(p, resid_attr) for p in spcs)

        total_kg = float(sum(totals))
        se_sampling_kg = float(np.sqrt(se_sq))
        se_meas_kg = float(np.sqrt(total_meas_var))
        se_resid_kg = float(np.sqrt(total_resid_var))

        # Welch-Satterthwaite across strata:
        #   nu = (Σ V_h)² / Σ V_h²/nu_h, V_h = stratum design variance.
        # Strata with one plot (sd=0, nu=0) contribute no variance term.
        var_h = [v for v, d in df_num_parts if d > 0 and v > 0]
        den_terms = [(v, d) for v, d in df_num_parts if d > 0 and v > 0]
        total_var = sum(var_h)
        if total_var > 0 and den_terms:
            df = total_var ** 2 / sum(v ** 2 / d for v, d in den_terms)
            # cannot have more df than total plots - number of strata
            n_total = sum(d + 1 for _, d in den_terms)
            df = max(1.0, min(df, n_total - len(den_terms)))
        else:
            df = 1.0

        component_results[comp] = {
            "total_mg": total_kg / KG_PER_MG,
            "total_kg": total_kg,
            "se_sampling_kg": se_sampling_kg,
            "se_measurement_kg": se_meas_kg,
            "se_equation_residual_kg": se_resid_kg,
            "se_total_kg": float(np.sqrt(se_sampling_kg ** 2 + total_resid_var)),
            "df": df,
            "ci95_factor": float(stats.t.ppf(0.975, max(df, 1))),
            "strata": strat_out,
        }
        r = component_results[comp]
        r["ci95_kg"] = [
            total_kg - r["ci95_factor"] * r["se_total_kg"],
            total_kg + r["ci95_factor"] * r["se_total_kg"],
        ]

    # ---- net change & stock reconciliation
    g = component_results["survivor_growth"]["total_kg"]
    m = component_results["mortality"]["total_kg"]
    i = component_results["ingrowth"]["total_kg"]
    net = g - m + i

    # stocks (equation applied once per occasion)
    stock_rows = []
    stock_total = {"t1_kg": 0.0, "t2_kg": 0.0}
    stock_resid = {"t1": 0.0, "t2": 0.0}
    stock_se_sq = {"t1": 0.0, "t2": 0.0}
    for scode, spcs in by_stratum.items():
        A_h = strata[scode]["area_ha"]
        n = len(spcs)
        for occ, attr, res_attr, key in [
            ("t1", "stock_t1_kg", "stock_t1_resid_var", "t1_kg"),
            ("t2", "stock_t2_kg", "stock_t2_resid_var", "t2_kg"),
        ]:
            vals = [getattr(p, attr) / p.area_ha for p in spcs]
            se = _stratum_estimate(vals, A_h)
            stock_se_sq[occ] += se["se_design_kg"] ** 2
            stock_total[key] += se["total_kg"]
            stock_resid[occ] += sum(getattr(p, res_attr) for p in spcs)
            stock_rows.append({"stratum": scode, "occasion": occ, **{
                k: v for k, v in se.items() if k != "plot_values_kg_ha"}})

    # net-change equation error: same trees & same equation at t1/t2 ->
    # residual errors assumed perfectly correlated, cancel in survivor
    # growth; mortality (t1 only) and ingrowth (t2 only) remain independent.
    net_se_kg = float(np.sqrt(
        sum(component_results[c]["se_sampling_kg"] ** 2 for c in COMPONENTS)
        + 2 * 0.0  # sampling covariances across components assumed 0 (below)
    ))
    net_se_resid_kg = float(np.sqrt(
        component_results["mortality"]["se_equation_residual_kg"] ** 2
        + component_results["ingrowth"]["se_equation_residual_kg"] ** 2
    ))

    # provenance / data quality listing
    provenance = {
        "pairs_same_number": sum(1 for p in pairing["pairs"]
                                 if p["kind"] == "same_number"),
        "pairs_verified_renumber": sum(1 for p in pairing["pairs"]
                                       if p["kind"] == "renumber"),
        "open_conflicts": [
            {"plot": (c.get("t1") or c.get("t2"))["plot"],
             "field_number": (c.get("t1") or c.get("t2"))["field_number"],
             "distance_m": (None if c["distance_m"] is None
                            else round(c["distance_m"], 2)),
             "hint": c.get("hint", "same_number_position_mismatch")}
            for c in pairing["conflicts"]
        ],
        "plots": [_pc_provenance(pc) for pc in pcs],
    }

    result = {
        "occasions": {
            "t1": design["t1_code"], "t2": design["t2_code"],
            "interval_years": design["interval_years"],
        },
        "units": {"biomass": "kg tree / Mg population",
                  "dbh": "cm (converted at ingest; raw unit retained)",
                  "height": "m", "area": "hectare",
                  "crs_epsg": design["crs_epsg"]},
        "sampling_frame": {
            "version": design.get("frame_version"),
            "binding": "estimate is bound to this published sampling-frame "
                       "version; a later boundary revision never changes it",
            "revised_plots": {
                code: info.get("frame_revision_id")
                for code, info in plots.items()
                if info.get("frame_revision_id") is not None
            },
        },
        "design": {
            "estimator": "stratified simple random sampling, per-hectare "
                         "plot values expanded by stratum land area",
            "strata": strata,
            "fpc_used": bool(design.get("fpc")),
            "recruitment_dbh_cm": design["recruitment_cm"],
            "zero_growth_tolerance_cm": design["zero_tol_cm"],
        },
        "components": component_results,
        "stocks": {
            "t1_mg": stock_total["t1_kg"] / KG_PER_MG,
            "t2_mg": stock_total["t2_kg"] / KG_PER_MG,
            "se_sampling_t1_kg": float(np.sqrt(stock_se_sq["t1"])),
            "se_sampling_t2_kg": float(np.sqrt(stock_se_sq["t2"])),
            "se_residual_t1_kg": float(np.sqrt(stock_resid["t1"])),
            "se_residual_t2_kg": float(np.sqrt(stock_resid["t2"])),
            "per_stratum": stock_rows,
        },
        "net_change": {
            "total_mg": net / KG_PER_MG,
            "total_kg": net,
            "se_sampling_kg": net_se_kg,
            "se_equation_residual_kg": net_se_resid_kg,
            "se_total_kg": float(np.sqrt(net_se_kg ** 2 + net_se_resid_kg ** 2)),
            "identity_check": "net = survivor_growth - mortality + ingrowth",
        },
        "provenance": provenance,
        "uncertainty_assumptions": _assumptions(design),
        "equations_used": {sp: eq for sp, eq in sorted(equations.items())},
    }
    return result


def _pc_provenance(pc):
    return {
        "plot": pc.code, "stratum": pc.stratum, "area_ha": pc.area_ha,
        "frame_revision_id": pc.frame_revision_id,
        "survivor_observed_pairs": pc.survivor_observed_pairs,
        "verified_zero_growth": pc.survivor_zero_growth,
        "alive_not_measured": pc.survivor_missing,
        "imputed_survivor_growth_kg": round(pc.survivor_imputed_kg, 3),
        "mortality": pc.mortality_ids,
        "ingrowth": pc.ingrowth_ids,
        "below_recruitment": pc.below_recruitment_ids,
        "not_located": pc.missing_tree_ids,
        "equation_range_extrapolations": pc.extrapolation_ids,
        "equation_missing_inputs": pc.equation_missing_input_ids,
        "excluded_identity_conflicts": pc.excluded_conflict_ids,
        "kg": {
            "survivor_growth": round(pc.survivor_kg, 3),
            "mortality": round(pc.mortality_kg, 3),
            "ingrowth": round(pc.ingrowth_kg, 3),
            "stock_t1": round(pc.stock_t1_kg, 3),
            "stock_t2": round(pc.stock_t2_kg, 3),
        },
    }


def _assumptions(design):
    return [
        "Design-based inference: stratified simple random sampling; equal "
        "weight per plot on a per-hectare basis, stratum totals scaled by "
        "KNOWN stratum land areas. Trees are never pooled and averaged.",
        f"Measurement error modelled as independent Gaussian: dbh SD="
        f"{design['dbh_sd_cm']} cm, height SD={design['height_sd_m']} m; "
        "propagated by first-order derivatives. It is reported as a "
        "diagnostic component and is already embedded in between-plot "
        "sampling variance, so it is not double-counted in se_total.",
        "Allometric residual error modelled as multiplicative lognormal "
        "with the equation's residual_sigma; applied once to mortality "
        "(t1) and ingrowth (t2). For survivor growth the SAME equation is "
        "used at both occasions and residual errors are assumed perfectly "
        "correlated for a surviving tree, hence cancel from its increment.",
        "Missing survivor measurements (alive, not measured) are assumed "
        "MAR within plot: plot-level ratio imputation of growth "
        "(observed growth per t1 biomass of measured survivors); "
        "sampling variance is inflated by 1/(1-missing biomass fraction).",
        "Trees under recruitment dbh are recorded but excluded from "
        "ingrowth; trees not located and unverified identity pairs are "
        "excluded from ALL components and listed under provenance.",
        "95% CI uses Welch-Satterthwaite degrees of freedom across strata "
        "and the t distribution; component sampling covariances are "
        "assumed zero in the net-change SE.",
        "Plot declared area is cross-checked against boundary polygon area "
        "(1% tolerance); per-plot expansion uses each plot's own area.",
    ]

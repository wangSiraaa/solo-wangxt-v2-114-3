"""
Cross-survey tree identity matching.

The field number is a *label*, not an identity. The matcher never assumes
"same number => same tree":

* SAME tracked tree row (internal tree_id) at both occasions -> pair. A
  changed label on that row is a verified renumber by construction.
* Same label across DIFFERENT tree rows at both occasions, or a reused
  label alongside the legitimate owner:
    - human-verified renumber -> pair as renumber;
    - human-verified distinct -> stay unmatched (t1 removal / t2 ingrowth);
    - otherwise nearest one-to-one geometry -> open IdentityConflict,
      both sides EXCLUDED from every component.
* Different label, different row, close geometry -> open conflict with
  hint "possible_renumber" (verify; never auto-merged).
* A label appearing on only ONE occasion is normal (mortality candidate
  at t1; ingrowth candidate at t2), NOT an identity conflict.
"""
from collections import defaultdict

POSITION_TOLERANCE_M = 1.0
RENUMBER_SEARCH_RADIUS_M = 2.0


def _distance_m(x1, y1, x2, y2):
    return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5


def pair_measurements(t1_rows, t2_rows, tolerance_m=POSITION_TOLERANCE_M,
                      search_radius_m=RENUMBER_SEARCH_RADIUS_M,
                      resolved_renumber_pairs=None,
                      resolved_distinct_pairs=None):
    resolved_renumber_pairs = resolved_renumber_pairs or set()
    resolved_distinct_pairs = resolved_distinct_pairs or set()

    t1_by_num = defaultdict(list)
    t2_by_num = defaultdict(list)
    for r in t1_rows:
        t1_by_num[(r["plot"], r["field_number"])].append(r)
    for r in t2_rows:
        t2_by_num[(r["plot"], r["field_number"])].append(r)

    pairs, conflicts = [], []
    paired_t1, paired_t2 = set(), set()
    excluded_t1, excluded_t2 = set(), set()

    # Pass 0: same tracked tree row at both occasions.
    t1_by_tree = {r["tree_id"]: r for r in t1_rows}
    for r2 in t2_rows:
        r1 = t1_by_tree.get(r2["tree_id"])
        if r1 is None:
            continue
        d = _distance_m(r1["x_m"], r1["y_m"], r2["x_m"], r2["y_m"])
        kind = ("same_number" if r1["field_number"] == r2["field_number"]
                else "renumber")
        pairs.append({"t1": r1, "t2": r2, "kind": kind, "distance_m": d})
        paired_t1.add(r2["tree_id"])
        paired_t2.add(r2["tree_id"])

    # Pass 1: same label carried by DIFFERENT tree rows.
    for key in set(t1_by_num) | set(t2_by_num):
        group1 = t1_by_num.get(key, [])
        group2 = t2_by_num.get(key, [])
        same_ids = {r["tree_id"] for r in group1} & {
            r["tree_id"] for r in group2}
        other1 = [r for r in group1 if r["tree_id"] not in same_ids]
        other2 = [r for r in group2 if r["tree_id"] not in same_ids]

        # Contradiction only if the label is contested across rows:
        # extra rows on both sides, or an extra row next to the legit owner.
        if not ((other1 and other2)
                or (same_ids and (other1 or other2))):
            continue

        decided1, decided2 = set(), set()
        distinct1, distinct2 = set(), set()
        for r1 in other1:
            for r2 in other2:
                pair_key = (r1["tree_id"], r2["tree_id"])
                d = _distance_m(r1["x_m"], r1["y_m"], r2["x_m"], r2["y_m"])
                if pair_key in resolved_renumber_pairs:
                    pairs.append({"t1": r1, "t2": r2, "kind": "renumber",
                                  "distance_m": d})
                    decided1.add(r1["tree_id"])
                    decided2.add(r2["tree_id"])
                elif pair_key in resolved_distinct_pairs:
                    # verified different individuals sharing a label:
                    # deliberately unmatched -> mortality/ingrowth paths
                    distinct1.add(r1["tree_id"])
                    distinct2.add(r2["tree_id"])

        open1 = [r for r in other1
                 if r["tree_id"] not in decided1 | distinct1]
        open2 = [r for r in other2
                 if r["tree_id"] not in decided2 | distinct2]

        edges = sorted(
            (_distance_m(r1["x_m"], r1["y_m"], r2["x_m"], r2["y_m"]), r1, r2)
            for r1 in open1 for r2 in open2
        )
        u1, u2 = set(), set()
        for d, r1, r2 in edges:
            if r1["tree_id"] in u1 or r2["tree_id"] in u2:
                continue
            conflicts.append({"t1": r1, "t2": r2, "distance_m": d,
                              "hint": "same_number_position_mismatch"})
            u1.add(r1["tree_id"])
            u2.add(r2["tree_id"])
        # Left-over extra rows are held for verification.
        for r1 in open1:
            if r1["tree_id"] not in u1:
                conflicts.append({"t1": r1, "t2": None, "distance_m": None,
                                  "hint": "same_label_extra_row"})
                excluded_t1.add(r1["tree_id"])
        for r2 in open2:
            if r2["tree_id"] not in u2:
                conflicts.append({"t1": None, "t2": r2, "distance_m": None,
                                  "hint": "same_label_extra_row"})
                excluded_t2.add(r2["tree_id"])
        excluded_t1 |= u1
        excluded_t2 |= u2
        paired_t1 |= decided1
        paired_t2 |= decided2

    # Pass 2: field-book verified renumber links (different labels).
    for r2 in t2_rows:
        if r2["tree_id"] in paired_t2 or r2["tree_id"] in excluded_t2:
            continue
        target = r2.get("verified_renumber_of")
        if target:
            r1 = next((r for r in t1_rows
                       if r["tree_id"] == target
                       and r["tree_id"] not in paired_t1
                       and r["tree_id"] not in excluded_t1), None)
            if r1:
                d = _distance_m(r1["x_m"], r1["y_m"], r2["x_m"], r2["y_m"])
                pairs.append({"t1": r1, "t2": r2, "kind": "renumber",
                              "distance_m": d})
                paired_t1.add(r1["tree_id"])
                paired_t2.add(r2["tree_id"])

    # Pass 3: possible unrecorded renumber (new label, close geometry).
    for r2 in t2_rows:
        if r2["tree_id"] in paired_t2 or r2["tree_id"] in excluded_t2:
            continue
        for r1 in t1_rows:
            if r1["tree_id"] in paired_t1 or r1["tree_id"] in excluded_t1:
                continue
            if r1["field_number"] == r2["field_number"]:
                continue
            d = _distance_m(r1["x_m"], r1["y_m"], r2["x_m"], r2["y_m"])
            if d <= search_radius_m:
                conflicts.append({"t1": r1, "t2": r2, "distance_m": d,
                                  "hint": "possible_renumber"})
                excluded_t1.add(r1["tree_id"])
                excluded_t2.add(r2["tree_id"])
                break

    t1_only = [r for r in t1_rows
               if r["tree_id"] not in paired_t1
               and r["tree_id"] not in excluded_t1]
    t2_only = [r for r in t2_rows
               if r["tree_id"] not in paired_t2
               and r["tree_id"] not in excluded_t2]

    return {"pairs": pairs, "conflicts": conflicts,
            "t1_only": t1_only, "t2_only": t2_only}

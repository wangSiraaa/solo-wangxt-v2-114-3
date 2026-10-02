"""
Scan two campaigns for identity conflicts and persist them.

A conflict = same field number in one plot at both campaigns but the
recorded stem positions contradict "same individual" (distance beyond
tolerance), or a close-position pair carrying different labels that might
be an unrecorded renumber. Nothing is merged automatically.
"""
from django.utils import timezone

from inventory.models import IdentityConflict
from inventory.services.identity import (
    POSITION_TOLERANCE_M,
    RENUMBER_SEARCH_RADIUS_M,
    _distance_m,
)


def scan_conflicts(t1_campaign, t2_campaign,
                   tolerance_m=POSITION_TOLERANCE_M,
                   search_radius_m=RENUMBER_SEARCH_RADIUS_M):
    from inventory.models import TreeMeasurement

    t1_qs = TreeMeasurement.objects.filter(
        campaign=t1_campaign
    ).select_related("tree", "tree__plot")
    t2_qs = TreeMeasurement.objects.filter(
        campaign=t2_campaign
    ).select_related("tree", "tree__plot")

    t1_by_plot_num, t2_by_plot_num = {}, {}
    for m in t1_qs:
        t1_by_plot_num.setdefault((m.tree.plot_id, m.field_number_seen), []).append(m)
    for m in t2_qs:
        t2_by_plot_num.setdefault((m.tree.plot_id, m.field_number_seen), []).append(m)

    found = []
    # same label, contradictory position. Iterate from the t2 side so an
    # EXTRA row reusing a label is caught even when the genuine t1 tree is
    # also present at its original position: each t2 row whose nearest
    # same-label t1 record is too far away is an unverified identity.
    for key, m2_list in t2_by_plot_num.items():
        for m2 in m2_list:
            t1_list = t1_by_plot_num.get(key, [])
            if not t1_list:
                continue
            nearest = None
            for m1 in t1_list:
                d = _distance_m(m1.x_m, m1.y_m, m2.x_m, m2.y_m)
                if nearest is None or d < nearest[0]:
                    nearest = (d, m1)
            d, m1 = nearest
            if d > tolerance_m and not _has_resolution(m1, m2):
                found.append(_upsert(t1_campaign, t2_campaign, m1, m2, d,
                                     "same_number_position_mismatch"))

    # different labels, close position -> possible unrecorded renumber,
    # unless the two records are already the SAME tracked tree (a verified
    # tag replacement) or a human decision exists.
    t2_by_plot = {}
    for m in t2_qs:
        t2_by_plot.setdefault(m.tree.plot_id, []).append(m)
    for m1 in t1_qs:
        for m2 in t2_by_plot.get(m1.tree.plot_id, []):
            if m1.tree_id == m2.tree_id:
                continue
            if m1.field_number_seen == m2.field_number_seen:
                continue
            d = _distance_m(m1.x_m, m1.y_m, m2.x_m, m2.y_m)
            if d <= search_radius_m and not _has_resolution(m1, m2):
                found.append(_upsert(t1_campaign, t2_campaign, m1, m2, d,
                                     "possible_renumber"))
    return found


def _has_resolution(m1, m2):
    return IdentityConflict.objects.filter(
        t1_measurement=m1, t2_measurement=m2,
        status__in=["renumber", "distinct"],
    ).exists()


def _upsert(t1_campaign, t2_campaign, m1, m2, distance, hint):
    obj, created = IdentityConflict.objects.get_or_create(
        t1_measurement=m1, t2_measurement=m2,
        defaults={
            "plot": m1.tree.plot,
            "field_number": m1.field_number_seen,
            "t1_campaign": t1_campaign,
            "t2_campaign": t2_campaign,
            "distance_m": round(distance, 3),
            "resolution_note": hint,
        },
    )
    return {"id": obj.id, "created": created,
            "plot": m1.tree.plot.code,
            "field_number": m1.field_number_seen,
            "t2_field_number": m2.field_number_seen,
            "distance_m": round(distance, 2),
            "hint": hint if created else obj.get_status_display()}

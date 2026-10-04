from django.urls import include, path
from rest_framework.routers import DefaultRouter

from inventory import views

router = DefaultRouter()
router.register("strata", views.StratumViewSet)
router.register("species", views.SpeciesViewSet)
router.register("campaigns", views.CampaignViewSet)
router.register("equations", views.EquationViewSet)
router.register("plots", views.PlotViewSet)
router.register("trees", views.TreeViewSet, basename="tree")
router.register("measurements", views.MeasurementViewSet,
                basename="measurement")
router.register("conflicts", views.ConflictViewSet, basename="conflict")
router.register("imports", views.ImportViewSet, basename="import")
router.register("estimates", views.EstimateViewSet, basename="estimate")
router.register("frames", views.SamplingFrameViewSet, basename="frame")
router.register("plot-revisions", views.PlotFrameRevisionViewSet,
                basename="plot-revision")

urlpatterns = [path("", include(router.urls))]

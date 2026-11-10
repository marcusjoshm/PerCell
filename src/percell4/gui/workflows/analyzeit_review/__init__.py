"""The AnalyzeIt review window (plan unit U9 of AnalyzeIt's harness plan).

A standalone controller with its own tool window, opened without PerCell's batch workflow
runner, like the dilute phase workflow. The logic lives in :mod:`.controller` on narrow
protocols; :mod:`.window` holds the Qt tool window and the napari renderer. The Qt-free
session is in ``percell4.workflows.analyzeit_review``.
"""

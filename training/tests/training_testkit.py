"""Shared helpers for the training tests (a unique name, so it never clashes with another
test folder's modules)."""

from training.data.build import BuildConfig

SMALL = BuildConfig(
    name="actions-test",
    worlds=["payments", "security"],
    templates=True,
    template_variants=1,
    stress_seeds=[0],
    max_modifiers=1,
    max_tasks=12,
)
"""Twelve public tasks per world, one variant of each template on them, single modifiers."""

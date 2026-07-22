"""NanoScope: Principal/Audience-aware multi-user Agent Runtime (魔改 nanobot).

See PRD.md / CLAUDE.md / PROGRESS.md at repo root.
"""

from nanoscope.identity import (
    AUDIENCE_DM,
    AUDIENCE_GROUP,
    AUDIENCE_THREAD,
    IdentityResolver,
    SecurityContext,
    TenantConflictError,
    audience_type_from_dm,
    make_audience_id,
    make_principal_id,
)

__all__ = [
    "AUDIENCE_DM",
    "AUDIENCE_GROUP",
    "AUDIENCE_THREAD",
    "IdentityResolver",
    "SecurityContext",
    "TenantConflictError",
    "audience_type_from_dm",
    "make_audience_id",
    "make_principal_id",
]

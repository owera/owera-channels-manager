"""Route-level enforcement of per-actor workspace scope (V3). docs/ACCESS_AUDIT.md.

Every included router gets ``Depends(enforce_actor_scope)``. For a token actor:
  * read-only actors: only GET/HEAD/OPTIONS;
  * role suffix (``owera+channels`` / ``+vm`` / ``admin``) must be in the actor's roles;
  * workspace-scoped routes (their dependency tree includes ``current_workspace``):
    the resolved slug must be in the actor's workspaces;
  * global/admin routes (no ``current_workspace``): only actors with scope "*".
Legacy-shared, SPA Google and open-dev requests keep today's behavior.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app import actor_auth

_ROUTES: dict = {}


def _walk_uses_current_workspace(dependant) -> bool:
    from app.workspace import current_workspace
    stack = list(dependant.dependencies)
    while stack:
        d = stack.pop()
        if d.call is current_workspace:
            return True
        stack.extend(d.dependencies)
    return False


def route_info(request: Request) -> tuple[str, bool]:
    """(route template, workspace_scoped) for the matched endpoint."""
    endpoint = request.scope.get("endpoint")
    app = request.scope.get("app")
    if endpoint is None or app is None:
        return "<unmatched>", False
    key = (id(app), endpoint)
    if key not in _ROUTES:
        from fastapi.routing import APIRoute
        for r in getattr(app, "routes", []):
            if isinstance(r, APIRoute) and r.endpoint is endpoint:
                _ROUTES[key] = (r.path, _walk_uses_current_workspace(r.dependant))
                break
        else:
            _ROUTES[key] = (getattr(endpoint, "__name__", "<endpoint>"), False)
    return _ROUTES[key]


def route_template(request: Request) -> str:
    if request.scope.get("endpoint") is None:
        return "<unmatched>"
    return route_info(request)[0]


def enforce_actor_scope(request: Request) -> None:
    actor = getattr(request.state, "actor", None)
    if actor is None or getattr(actor, "method", None) != "token":
        return
    from app.workspace import basic_auth_identity, resolve_workspace_slug
    _template, scoped = route_info(request)
    ident = basic_auth_identity(request)
    role = ident[1] if ident and ident[1] in actor_auth.ROLE_ACTORS else None
    try:
        slug = resolve_workspace_slug(request)
    except HTTPException:
        slug = None
    reason = actor_auth.scope_denial(actor, workspace_slug=slug, role=role,
                                     workspace_scoped=scoped, method=request.method)
    if reason:
        request.state.audit_reason = reason
        raise HTTPException(403, reason)

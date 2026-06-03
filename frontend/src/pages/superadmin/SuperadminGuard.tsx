import { Navigate, Outlet } from "react-router";
import { useAuth } from "../../hooks/useAuth";

/**
 * Route-level gate for ``/superadmin/*``.
 *
 * Non-superadmins are bounced to ``/`` — same shape as React Router's
 * catch-all redirect, so the surface looks like it doesn't exist to
 * regular users. The backend independently returns 403 to anyone not
 * on ``MCPOLIS_SUPERADMIN_EMAILS``, so the guard is defense-in-depth.
 */
export function SuperadminGuard() {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user?.is_superadmin) return <Navigate to="/" replace />;
  return <Outlet />;
}

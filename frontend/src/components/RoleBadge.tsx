import { Link } from "react-router";

export function RoleBadge({
  role,
  isAdmin,
}: {
  role: string;
  isAdmin?: boolean;
}) {
  const label = role === "default" ? "Default" : role;
  return (
    <Link
      to={`/admin/permissions?role=${encodeURIComponent(role)}`}
      className={`inline-block px-1.5 py-0.5 rounded text-xs hover:opacity-80 ${
        isAdmin
          ? "bg-purple-100 text-purple-700"
          : "bg-zinc-100 text-zinc-600"
      }`}
    >
      {label}
    </Link>
  );
}

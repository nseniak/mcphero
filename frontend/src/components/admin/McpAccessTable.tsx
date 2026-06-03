import { Link } from "react-router";
import { ChevronRight } from "lucide-react";
import type { UpstreamSummary } from "../../api/types";
import SettingToggle from "../ui/setting-toggle";

export interface McpAccessRow {
  mcpId: string;
  displayName: string;
  value: boolean;
  toolCount?: number;
}

export function McpAccessTable({
  upstreams,
  rows,
  onChange,
  emptyMessage,
  bordered = true,
  onToolsClick,
}: {
  upstreams: UpstreamSummary[];
  rows: McpAccessRow[];
  onChange: (mcpId: string, value: boolean) => void;
  emptyMessage: string;
  bordered?: boolean;
  onToolsClick?: (mcpId: string) => void;
}) {
  if (upstreams.length === 0) {
    return <p className="text-zinc-400 text-sm">{emptyMessage}</p>;
  }

  const showToolsColumn = onToolsClick && rows.some((r) => r.toolCount != null);

  return (
    <div className={bordered ? "border border-zinc-200 rounded-lg overflow-hidden" : ""}>
      <table className="w-full text-sm">
        <thead className="bg-zinc-50 text-zinc-600">
          <tr>
            <th className="text-left px-4 py-2 font-medium">Name</th>
            {showToolsColumn && <th className="text-center px-4 py-2 font-medium">Tools</th>}
            <th className="text-right px-4 py-2 font-medium">Enabled</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.mcpId} className="border-t border-zinc-100 hover:bg-zinc-50">
              <td className="px-4 py-2">
                {onToolsClick ? (
                  <button
                    onClick={() => onToolsClick(row.mcpId)}
                    className={`hover:underline text-left font-medium cursor-pointer ${row.value ? "text-blue-600" : "text-zinc-400"}`}
                  >
                    {row.displayName}
                  </button>
                ) : (
                  <Link
                    to={`/admin/upstream/${row.mcpId}`}
                    className={`hover:underline font-medium ${row.value ? "text-blue-600" : "text-zinc-400"}`}
                  >
                    {row.displayName}
                  </Link>
                )}
              </td>
              {showToolsColumn && (
                <td className="px-4 py-2 text-center">
                  <button
                    onClick={() => onToolsClick!(row.mcpId)}
                    className="inline-flex items-center gap-0.5 text-blue-500 hover:text-blue-700 hover:underline cursor-pointer text-sm font-medium"
                  >
                    {row.toolCount ?? 0}
                    <ChevronRight size={12} />
                  </button>
                </td>
              )}
              <td className="px-4 py-2 text-right">
                <div className="inline-block">
                  <SettingToggle
                    value={row.value}
                    onChange={(v) => onChange(row.mcpId, v)}
                  />
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

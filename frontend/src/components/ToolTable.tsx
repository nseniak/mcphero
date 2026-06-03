import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { ToolInfo, ToolAnnotationsInfo } from "../api/types";
import { useTranslation } from "../i18n/index";

function ToolParamList({ schema }: { schema: Record<string, unknown> }) {
  const properties = schema.properties as Record<string, Record<string, unknown>> | undefined;
  const required = new Set((schema.required as string[] | undefined) ?? []);
  if (!properties || Object.keys(properties).length === 0) return null;

  return (
    <div className="mt-2">
      <div className="text-xs font-medium text-zinc-500 mb-1">Parameters</div>
      <div className="space-y-1">
        {Object.entries(properties).map(([name, prop]) => {
          const type = (prop.type as string) ?? (prop.enum ? "enum" : "any");
          const desc = prop.description as string | undefined;
          const isRequired = required.has(name);
          return (
            <div key={name} className="text-xs">
              <span className="font-mono text-zinc-700">{name}</span>
              <span className="text-zinc-400 ml-1">{type}</span>
              {isRequired && <span className="text-red-400 ml-1">*</span>}
              {desc && <span className="text-zinc-500 ml-2">{desc}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function ToolAnnotationBadges({ annotations }: { annotations: ToolAnnotationsInfo }) {
  const badges: { label: string; color: string }[] = [];
  if (annotations.destructiveHint) badges.push({ label: "destructive", color: "bg-red-100 text-red-700" });
  if (annotations.readOnlyHint) badges.push({ label: "read-only", color: "bg-green-100 text-green-700" });
  if (annotations.idempotentHint) badges.push({ label: "idempotent", color: "bg-blue-100 text-blue-700" });
  if (annotations.openWorldHint) badges.push({ label: "open-world", color: "bg-amber-100 text-amber-700" });
  if (badges.length === 0) return null;
  return (
    <div className="flex gap-1 mt-0.5">
      {badges.map((b) => (
        <span key={b.label} className={`px-1 py-0 rounded text-[9px] ${b.color}`}>
          {b.label}
        </span>
      ))}
    </div>
  );
}

export function ToolRow({ tool }: { tool: ToolInfo }) {
  const [expanded, setExpanded] = useState(false);
  const description = tool.description ?? "—";
  const displayName = tool.title ?? tool.original_name;

  return (
    <>
      <tr
        className="hover:bg-zinc-50 cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <td className="px-4 py-2 font-mono text-xs align-top">
          <div className="flex items-center gap-1.5">
            {expanded ? <ChevronDown size={12} className="text-zinc-400" /> : <ChevronRight size={12} className="text-zinc-400" />}
            {displayName}
          </div>
          {tool.annotations && <div className="pl-[20px]"><ToolAnnotationBadges annotations={tool.annotations} /></div>}
        </td>
        <td className="px-4 py-2 text-zinc-600">
          {expanded ? description : <span className="line-clamp-1">{description}</span>}
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={2} className="px-4 pt-0 pb-3 pl-10 space-y-2">
            {tool.title && tool.title !== tool.original_name && (
              <div className="text-xs text-zinc-400">
                ID: <span className="font-mono">{tool.original_name}</span>
              </div>
            )}
            <ToolParamList schema={tool.input_schema} />
          </td>
        </tr>
      )}
    </>
  );
}

export function ToolTable({ tools }: { tools: ToolInfo[] }) {
  const { t } = useTranslation();

  if (tools.length === 0) {
    return <p className="text-zinc-400 text-sm">{t("upstreamDetail.noTools")}</p>;
  }

  return (
    <div className="border border-zinc-200 rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-zinc-50 text-zinc-600">
          <tr>
            <th className="text-left px-4 py-2 font-medium w-[30%]">
              {t("upstreamDetail.headerToolName")}
            </th>
            <th className="text-left px-4 py-2 font-medium">
              {t("upstreamDetail.headerDescription")}
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100">
          {tools.map((tool) => (
            <ToolRow key={tool.prefixed_name} tool={tool} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

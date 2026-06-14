import type { SandboxCapabilitiesResponse, SandboxResourceCombo } from "../api/types";
import { isDockerCommand } from "../lib/sandbox-combo";

interface SandboxCombo {
  cpu_vcpus: number;
  memory_mb: number;
  disk_gb: number;
}

interface SandboxComboSelectProps {
  capabilities: SandboxCapabilitiesResponse;
  value: SandboxCombo;
  onChange: (combo: SandboxCombo) => void;
  hasError?: boolean;
  borderDefault?: string;
  /** The stdio command for this MCP. When it's the docker runner and a
   * 1-vCPU size is selected, a non-blocking below-floor warning renders
   * under the picker (E2B's documented Docker floor is 2 vCPU / 2 GB). */
  command?: string;
}

function comboKey(c: SandboxCombo): string {
  return `${c.cpu_vcpus}|${c.memory_mb}|${c.disk_gb}`;
}

function formatCombo(
  c: SandboxCombo,
  showDisk: boolean,
): string {
  const cpu = `${c.cpu_vcpus} vCPU`;
  const ram =
    c.memory_mb >= 1024 && c.memory_mb % 1024 === 0
      ? `${c.memory_mb / 1024} GiB`
      : `${c.memory_mb} MiB`;
  // Only surface the disk segment when it carries a real value — a
  // provider with no configurable disk reports 0, and "0 GiB disk"
  // reads as broken.
  const disk = showDisk && c.disk_gb > 0 ? ` / ${c.disk_gb} GiB disk` : "";
  return `${cpu} / ${ram}${disk}`;
}

export function SandboxComboSelect({
  capabilities,
  value,
  onChange,
  hasError = false,
  borderDefault = "border-zinc-300",
  command,
}: SandboxComboSelectProps) {
  // Only treat disk as configurable when some combo actually carries a
  // non-zero value; a flat [0] list means "no disk axis" and must not
  // render as "0 GiB disk".
  const showDisk = capabilities.allowed_disk_gb.some((d) => d > 0);
  const enforced = capabilities.enforces_resources;
  const currentKey = comboKey(value);
  const dockerBelowFloor =
    enforced && isDockerCommand(command) && value.cpu_vcpus < 2;

  return (
    <>
      <select
        value={currentKey}
        disabled={!enforced}
        onChange={(e) => {
          const picked = capabilities.allowed_combinations.find(
            (c: SandboxResourceCombo) => comboKey(c) === e.target.value,
          );
          if (!picked) return;
          onChange({
            cpu_vcpus: picked.cpu_vcpus,
            memory_mb: picked.memory_mb,
            disk_gb: picked.disk_gb,
          });
        }}
        className={`max-w-md w-full px-2 py-1 text-sm border rounded bg-white ${
          !enforced ? "opacity-60 cursor-not-allowed bg-zinc-50 " : ""
        }${hasError ? "border-red-400" : borderDefault}`}
      >
        {capabilities.allowed_combinations.map((c: SandboxResourceCombo) => (
          <option
            key={comboKey(c)}
            value={comboKey(c)}
            disabled={c.enabled === false}
          >
            {formatCombo(c, showDisk)}
            {c.enabled === false ? " — Team plan" : ""}
          </option>
        ))}
      </select>
      {!enforced && (
        <p className="mt-1.5 text-xs text-zinc-500">
          Not enforced in local mode — this MCP runs as a host subprocess
          and ignores these limits. Configure a sandbox provider (E2B) to
          enforce CPU and memory.
        </p>
      )}
      {dockerBelowFloor && (
        <p className="mt-1.5 text-xs text-amber-600">
          Docker's recommended minimum is 2 vCPU / 2 GB — the daemon alone
          uses ~150–250 MB. At 1 vCPU this MCP may run out of memory on any
          non-trivial image. You can still save it.
        </p>
      )}
    </>
  );
}

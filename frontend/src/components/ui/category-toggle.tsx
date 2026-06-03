import { cn } from "../../lib/utils"

export interface CategoryToggleProps {
  /** true = all on, false = all off, null = per tool */
  value: boolean | null
  onChange: (value: boolean | null) => void
  disabled?: boolean
}

const GREEN = "#1D9E75"
const AMBER = "rgba(217, 157, 42, 0.8)"

export default function CategoryToggle({
  value,
  onChange,
  disabled = false,
}: CategoryToggleProps) {
  // 3 positions: off=0, per-tool=1, on=2
  const trackWidth = 48
  const thumbSize = 14
  const padding = 3

  const positionIndex = value === false ? 0 : value === null ? 1 : 2

  const maxTravel = trackWidth - thumbSize - padding * 2
  const thumbOffset = (positionIndex / 2) * maxTravel

  // Track color: off=gray, per-tool=amber, on=green
  const trackColor = value === true ? GREEN : value === null ? AMBER : undefined
  const trackBorderColor = value === true ? GREEN : value === null ? AMBER : undefined

  const labels: Record<string, string> = {
    off: "All off",
    perTool: "Per tool",
    on: "All on",
  }
  const label = value === false ? labels.off : value === null ? labels.perTool : labels.on

  function handleClick() {
    if (disabled) return
    // Cycle: off → per-tool → on → off
    if (value === false) {
      onChange(null)
    } else if (value === null) {
      onChange(true)
    } else {
      onChange(false)
    }
  }

  return (
    <div className={cn(
      "inline-flex items-center gap-2",
      disabled && "opacity-50 pointer-events-none",
    )}>
      <span className={cn(
        "text-[11px] whitespace-nowrap select-none min-w-[100px] text-right",
        "text-foreground font-medium",
      )}>
        {label}
      </span>
      <button
        type="button"
        aria-label={label}
        disabled={disabled}
        onClick={handleClick}
        className={cn(
          "relative shrink-0 rounded-full border-[1.5px] transition-colors duration-200",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          !disabled && "cursor-pointer",
        )}
        style={{
          width: trackWidth,
          height: 20,
          backgroundColor: trackColor ?? "var(--color-muted-foreground, #888)",
          borderColor: trackBorderColor ?? "rgba(128,128,128,0.3)",
          opacity: value === false ? 0.3 : undefined,
        }}
      >
        <span
          className="absolute top-1/2 block rounded-full bg-white shadow-sm transition-all duration-200"
          style={{
            width: thumbSize,
            height: thumbSize,
            transform: `translate(${thumbOffset}px, -50%)`,
            left: padding,
          }}
        />
        {/* Position dots */}
        {[0, 1, 2].map((i) => {
          const dotOffset = padding + thumbSize / 2 - 1.5 + (i / 2) * maxTravel
          if (i === positionIndex) return null
          return (
            <span
              key={i}
              className="absolute top-1/2 block rounded-full bg-white/40"
              style={{
                width: 3,
                height: 3,
                transform: "translateY(-50%)",
                left: dotOffset,
              }}
            />
          )
        })}
      </button>
    </div>
  )
}

import { cn } from "../../lib/utils"

export interface SettingToggleProps {
  value: boolean
  onChange: (value: boolean) => void
  disabled?: boolean
  /** Hide the text label, showing only the toggle switch. */
  hideLabel?: boolean
}

const GREEN = "#1D9E75"

export default function SettingToggle({
  value,
  onChange,
  disabled = false,
  hideLabel = false,
}: SettingToggleProps) {
  const trackWidth = 36
  const thumbSize = 14
  const padding = 3

  const positionIndex = value ? 1 : 0
  const maxTravel = trackWidth - thumbSize - padding * 2
  const thumbOffset = positionIndex * maxTravel

  const trackColor = value ? GREEN : undefined
  const trackBorderColor = value ? GREEN : undefined

  const label = value ? "On" : "Off"

  function handleClick() {
    if (disabled) return
    onChange(!value)
  }

  return (
    <div className={cn(
      "inline-flex items-center gap-2",
      disabled && "opacity-50 pointer-events-none",
    )}>
      {!hideLabel && (
        <span className={cn(
          "text-[11px] whitespace-nowrap select-none min-w-[20px] text-right",
          "text-foreground font-medium",
        )}>
          {label}
        </span>
      )}
      <button
        type="button"
        role="switch"
        aria-checked={value}
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
          opacity: !value ? 0.3 : undefined,
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
      </button>
    </div>
  )
}

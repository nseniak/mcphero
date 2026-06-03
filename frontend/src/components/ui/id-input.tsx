import { useState } from "react"
import { cn } from "../../lib/utils"
import { FieldHint } from "./field-hint"

const ID_REGEX = /[^a-z0-9\-_.]/g

export interface IdInputProps {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  className?: string
  autoFocus?: boolean
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void
  inputRef?: React.Ref<HTMLInputElement>
}

export default function IdInput({
  value,
  onChange,
  placeholder = "my-identifier",
  className,
  autoFocus,
  onKeyDown,
  inputRef,
}: IdInputProps) {
  const [showHint, setShowHint] = useState(false)

  return (
    <div>
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => {
          const raw = e.target.value
          const sanitized = raw.toLowerCase().replace(ID_REGEX, "")
          setShowHint(raw !== sanitized)
          onChange(sanitized)
        }}
        placeholder={placeholder}
        autoFocus={autoFocus}
        onKeyDown={onKeyDown}
        className={cn(
          "px-2.5 py-1.5 border border-zinc-200 rounded text-sm bg-white focus:outline-none focus:ring-1 focus:ring-blue-400",
          className,
        )}
      />
      {showHint && (
        <FieldHint>Allowed: lowercase letters, digits, hyphens, underscores, dots</FieldHint>
      )}
    </div>
  )
}

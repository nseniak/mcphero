import { useTranslation, type TranslationKey } from "../i18n/index";

const AUTH_MODE_STYLE = "bg-blue-50 text-blue-700";

interface AuthBadgeProps {
  authMode: string;
}

export function AuthBadge({ authMode }: AuthBadgeProps) {
  const { t } = useTranslation();
  const style = AUTH_MODE_STYLE;
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs ${style}`}>
      {t(`authMode.${authMode}` as TranslationKey)}
    </span>
  );
}

import { createContext, useContext } from "react";
import { en } from "./en";

export type TranslationKey = keyof typeof en;

type TranslateFunction = (
  key: TranslationKey,
  params?: Record<string, string | number>,
) => string;

function makeT(translations: typeof en): TranslateFunction {
  return (key, params) => {
    let value: string = translations[key];
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        value = value.replaceAll(`{{${k}}}`, String(v));
      }
    }
    return value;
  };
}

const defaultT = makeT(en);

interface I18nContextValue {
  t: TranslateFunction;
}

export const I18nContext = createContext<I18nContextValue>({ t: defaultT });

export function I18nProvider({ children }: { children: React.ReactNode }): React.ReactNode {
  return (
    <I18nContext.Provider value={{ t: defaultT }}>
      {children}
    </I18nContext.Provider>
  );
}

export function useTranslation() {
  return useContext(I18nContext);
}

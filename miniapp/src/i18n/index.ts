import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { en, ru } from "./locales";

const saved =
  typeof localStorage !== "undefined"
    ? localStorage.getItem("pf-lang")
    : null;

void i18n.use(initReactI18next).init({
  resources: {
    ru: { translation: ru },
    en: { translation: en },
  },
  lng: saved === "en" || saved === "ru" ? saved : "ru",
  fallbackLng: "ru",
  interpolation: { escapeValue: false },
});

export default i18n;

export function setAppLanguage(lng: "ru" | "en") {
  localStorage.setItem("pf-lang", lng);
  void i18n.changeLanguage(lng);
}

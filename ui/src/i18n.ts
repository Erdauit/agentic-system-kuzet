// Язык интерфейса: ru (по умолчанию) и kk (SPEC.md §0). Переводы kk требуют вычитки носителем.

export type Lang = "ru" | "kk";

const ru = {
  appTitle: "Qoima Vision",
  "nav.setup": "Мастер настройки",
  "nav.operations": "Оперативная обстановка",
  "nav.health": "Состояние системы",
  "nav.chat": "Чат",
  "nav.models": "Модели",
  "nav.tasks": "Задачи обучения",
  "nav.labeling": "Разметка",
  "nav.settings": "Настройки",
  phase: "Фаза",
  "health.title": "Состояние системы",
  "health.overall": "Общий статус",
  "health.updated": "Обновлено",
  "health.unreachable": "core-api недоступен — данные о состоянии не получены",
  "health.loading": "Загрузка…",
  "health.latency": "мс",
  "status.green": "Норма",
  "status.yellow": "Внимание",
  "status.red": "Авария",
  "status.unknown": "Нет данных",
  "status.not_deployed": "Не развёрнуто",
  "lang.ru": "Рус",
  "lang.kk": "Қаз",
} as const;

export type MessageKey = keyof typeof ru;

const kk: Record<MessageKey, string> = {
  appTitle: "Qoima Vision",
  "nav.setup": "Баптау шебері",
  "nav.operations": "Жедел жағдай",
  "nav.health": "Жүйе күйі",
  "nav.chat": "Чат",
  "nav.models": "Модельдер",
  "nav.tasks": "Оқыту тапсырмалары",
  "nav.labeling": "Белгілеу",
  "nav.settings": "Баптаулар",
  phase: "Кезең",
  "health.title": "Жүйе күйі",
  "health.overall": "Жалпы күй",
  "health.updated": "Жаңартылды",
  "health.unreachable": "core-api қолжетімсіз — күй туралы деректер алынбады",
  "health.loading": "Жүктелуде…",
  "health.latency": "мс",
  "status.green": "Қалыпты",
  "status.yellow": "Назар аударыңыз",
  "status.red": "Апат",
  "status.unknown": "Дерек жоқ",
  "status.not_deployed": "Орнатылмаған",
  "lang.ru": "Рус",
  "lang.kk": "Қаз",
};

const dictionaries: Record<Lang, Record<MessageKey, string>> = { ru, kk };

export function translate(lang: Lang, key: MessageKey): string {
  return dictionaries[lang][key];
}

const STORAGE_KEY = "qoima.lang";

export function initialLang(): Lang {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "ru" || saved === "kk") return saved;
  } catch {
    // localStorage может быть недоступен (приватный режим) — используем язык по умолчанию.
  }
  return "ru";
}

export function saveLang(lang: Lang): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, lang);
  } catch {
    // не критично: язык просто не запомнится
  }
}
